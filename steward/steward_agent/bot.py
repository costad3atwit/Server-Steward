"""Discord layer: DM-only, owner-only, ✅/❌ confirmation for risky actions."""

from __future__ import annotations

import asyncio
import logging

import discord

from .agent import Agent, Proposal

log = logging.getLogger("steward.bot")

YES, NO = "✅", "❌"


class StewardBot(discord.Client):
    def __init__(self, agent: Agent, owner_ids: set[int]):
        intents = discord.Intents.none()
        intents.dm_messages = True        # DM content is delivered without the privileged intent
        intents.dm_reactions = True
        super().__init__(intents=intents)
        self.agent = agent
        self.owner_ids = owner_ids

    async def dm_owners(self, text: str) -> None:
        """Used by the help endpoint to forward questions it can't answer."""
        await self.wait_until_ready()
        for uid in self.owner_ids:
            user = self.get_user(uid) or await self.fetch_user(uid)
            await user.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s); owners: %s", self.user, self.user.id, sorted(self.owner_ids))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        rid = f"discord-{message.id}"
        if message.author.id not in self.owner_ids:
            self.agent.audit(event="ignored", request_id=rid, user_id=message.author.id,
                             reason="not an owner")
            return
        text = message.content.strip()[:1000]
        if not text:
            return

        await self._handle(message, text, rid)

    async def _handle(self, message: discord.Message, text: str, rid: str) -> None:
        # Only the model call is serialized; waiting for a ✅ doesn't block other requests.
        if self.agent.llm_lock.locked():
            await message.reply("Still thinking about another request, one moment…")
        if not await self.agent.model_is_warm():
            await message.channel.send("🥱 Waking up the model, this takes about 30 seconds…")
        async with message.channel.typing():
            plan = await self.agent.plan(text, rid)

        if not isinstance(plan, Proposal):
            await message.reply(plan.text[:1900])
            return
        if not plan.needs_confirmation:
            async with message.channel.typing():
                await message.reply((await self.agent.execute(plan))[:1900])
            return
        await self._confirm_and_run(message, plan)

    async def _confirm_and_run(self, message: discord.Message, plan: Proposal) -> None:
        timeout = self.agent.cfg.confirm_timeout_seconds
        prompt = await message.reply(
            f"About to {plan.summary}.\nReact {YES} to go ahead or {NO} to cancel "
            f"(expires in {timeout // 60} min)."
        )
        await prompt.add_reaction(YES)
        await prompt.add_reaction(NO)

        def check(ev: discord.RawReactionActionEvent) -> bool:
            return (ev.message_id == prompt.id and ev.user_id == message.author.id
                    and str(ev.emoji) in (YES, NO))

        try:
            ev = await self.wait_for("raw_reaction_add", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            self.agent.audit(event="confirm", request_id=plan.request_id, outcome="timeout")
            await prompt.edit(content=f"~~About to {plan.summary}.~~ Timed out, nothing was done.")
            return
        if str(ev.emoji) == NO:
            self.agent.audit(event="confirm", request_id=plan.request_id, outcome="cancelled")
            await prompt.edit(content=f"~~About to {plan.summary}.~~ Cancelled.")
            return

        self.agent.audit(event="confirm", request_id=plan.request_id, outcome="approved")
        await prompt.edit(content=f"Running: {plan.summary}…")
        async with message.channel.typing():
            result = await self.agent.execute(plan)
        await message.reply(result[:1900])
