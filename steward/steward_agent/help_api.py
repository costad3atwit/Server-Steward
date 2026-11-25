"""POST /help for a homepage help box. Untrusted input: help docs only, no tools, rate
limited, and questions it can't answer are forwarded to the owner on Discord."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import Agent


class HelpRequest(BaseModel):
    question: str


class ContactRequest(BaseModel):
    name: str
    contact: str = ""
    message: str


def escape_discord(text: str) -> str:
    """Untrusted text into a Discord DM: no markdown tricks, no mentions."""
    for ch in "\\*_~`|>#[]()":
        text = text.replace(ch, "\\" + ch)
    return text.replace("@", "@\u200b")


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window_seconds, clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def client_key(request: Request) -> str:
    # Steward's port is bound to localhost and sits behind the owner's reverse proxy, so
    # the first X-Forwarded-For entry is the real client.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def create_help_app(agent: Agent, forward: Callable[[str], Awaitable[None]],
                    clock: Callable[[], float] = time.monotonic) -> FastAPI:
    app = FastAPI(title="Server Steward help", docs_url=None, redoc_url=None, openapi_url=None)
    per_ip = SlidingWindow(agent.cfg.help.rate_limit_per_minute, 60, clock)
    forwards = SlidingWindow(agent.cfg.help.max_forwards_per_hour, 3600, clock)
    contact_limit = SlidingWindow(agent.cfg.help.contact_messages_per_hour, 3600, clock)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "help_chunks": agent.indexes["help"].size if "help" in agent.indexes else 0}

    @app.get("/help/status")
    async def help_status():
        """Lets the page say "waking up" before a cold first answer."""
        if not agent.cfg.help.enabled:
            return JSONResponse(status_code=404, content={"error": "help is disabled"})
        return {"ready": await agent.model_is_warm()}

    @app.post("/message")
    async def contact(body: ContactRequest, request: Request):
        hcfg = agent.cfg.help
        if not hcfg.contact_form:
            return JSONResponse(status_code=404, content={"error": "the contact form is disabled"})
        name = " ".join(body.name.split())
        contact_info = " ".join(body.contact.split())
        message = body.message.strip()
        if not name or not message:
            return JSONResponse(status_code=400, content={"error": "Please fill in your name and message."})
        if len(name) > 100 or len(contact_info) > 200 or len(message) > 2000:
            return JSONResponse(status_code=400, content={
                "error": "That's a bit long. Please keep the message under 2000 characters."})
        if not contact_limit.allow(client_key(request)):
            return JSONResponse(status_code=429, content={
                "error": "You've sent several messages already. Please wait a while before sending another."})
        rid = f"msg-{uuid.uuid4().hex[:12]}"
        quoted = "\n".join(f"> {line}" for line in escape_discord(message).splitlines() or [""])
        text = (f"📨 **Message from the help page**\n**From:** {escape_discord(name)}\n"
                f"**Contact:** {escape_discord(contact_info) or '(not given)'}\n{quoted}")
        try:
            await forward(text)
        except Exception as exc:
            agent.audit(event="contact_failed", request_id=rid, error=str(exc))
            return JSONResponse(status_code=502, content={
                "error": f"Couldn't send your message right now. Please contact {hcfg.owner_name} another way."})
        agent.audit(event="contact_sent", request_id=rid, name=name, chars=len(message))
        return {"ok": True}

    @app.post("/help")
    async def help_endpoint(body: HelpRequest, request: Request):
        hcfg = agent.cfg.help
        if not hcfg.enabled:
            return JSONResponse(status_code=404, content={"error": "help is disabled"})
        question = " ".join(body.question.split())   # collapse whitespace and newlines
        if not question:
            return JSONResponse(status_code=400, content={"error": "Please type a question."})
        if len(question) > hcfg.max_question_chars:
            return JSONResponse(status_code=400, content={
                "error": f"Please keep questions under {hcfg.max_question_chars} characters."})
        key = client_key(request)
        if not per_ip.allow(key):
            return JSONResponse(status_code=429, content={
                "error": "Too many questions at once. Please wait a minute and try again."})

        rid = f"help-{uuid.uuid4().hex[:12]}"
        result = await agent.answer_help(question, rid)

        if result.forwarded:
            if forwards.allow("global"):
                try:
                    await forward(f"❓ **Help question** ({result.reason.replace('_', ' ')})\n> {escape_discord(question)}")
                    agent.audit(event="help_forwarded", request_id=rid)
                except Exception as exc:  # Discord down: still answer the user
                    agent.audit(event="help_forward_failed", request_id=rid, error=str(exc))
            else:
                agent.audit(event="help_forward_suppressed", request_id=rid, reason="hourly limit")

        return {
            "answer": result.answer,
            "sources": [{"title": s["title"]} for s in result.sources],
            "answered": result.reason == "answered",
            "forwarded": result.forwarded,
        }

    return app
