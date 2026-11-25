"""Discord-independent core: text in, a Reply or a Proposal out. The eval runner (phase 7)
drives this class directly."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .clients import ExecutorClient, LLMError, OllamaClient
from .config import AgentConfig
from .formatting import format_failure, format_result
from .prompts import ADMIN_SYSTEM_PROMPT, HELP_SYSTEM_PROMPT, admin_notes_message, help_user_message
from .grounding import cited_sources, ground_urls
from .rag import DocIndex, Hit
from .tools import ToolError, ToolSpec, build_tools, duration_hours_in, validate_call

log = logging.getLogger("steward.agent")

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


@dataclass
class Reply:
    text: str


@dataclass
class Proposal:
    spec: ToolSpec
    args: dict
    request_id: str
    summary: str = field(init=False)
    verb: str = field(init=False)
    exec_args: dict = field(init=False)
    risk: str = field(init=False)

    def __post_init__(self):
        self.summary = self.spec.describe(self.args)
        self.verb, self.exec_args, self.risk = self.spec.resolve(self.args)

    @property
    def needs_confirmation(self) -> bool:
        return self.risk == "confirm"


@dataclass
class HelpAnswer:
    answer: str
    sources: list[dict]
    forwarded: bool
    reason: str          # "answered" | "no_docs" | "unsure" | "error"


class Agent:
    def __init__(self, cfg_loader: Callable[[], AgentConfig], llm: OllamaClient,
                 executor: ExecutorClient, tz: ZoneInfo, audit_path: Path | None = None,
                 docs_dir: Path | None = None, index_dir: Path | None = None):
        self._cfg_loader = cfg_loader
        self.llm = llm
        self.executor = executor
        self.tz = tz
        self.audit_path = audit_path
        self.docs_dir = docs_dir
        self.index_dir = index_dir
        # One model call at a time: admin DMs and help questions share a small GPU.
        self.llm_lock = asyncio.Lock()
        self.indexes: dict[str, DocIndex] = {}
        self.reload()

    def reload(self) -> None:
        self.cfg = self._cfg_loader()
        self.tools = build_tools(self.cfg)
        if self.docs_dir is not None and self.index_dir is not None:
            r = self.cfg.rag

            async def embedder(texts: list[str]) -> list[list[float]]:
                return await self.llm.embed(self.cfg.llm.embed_model, texts, self.cfg.rag.embed_options)

            for name in ("admin", "help"):
                self.indexes[name] = DocIndex(
                    name, self.docs_dir / name, self.index_dir / f"{name}.json", embedder,
                    model=self.cfg.llm.embed_model, doc_prefix=r.doc_prefix, query_prefix=r.query_prefix)

    async def refresh_indexes(self) -> None:
        for idx in self.indexes.values():
            try:
                if await idx.ensure_fresh():
                    self.audit(event="reindex", collection=idx.name, chunks=idx.size)
            except LLMError as exc:
                log.warning("could not index %s docs: %s", idx.name, exc)

    async def retrieve(self, collection: str, query: str) -> list[Hit]:
        idx = self.indexes.get(collection)
        if idx is None:
            return []
        try:
            return await idx.search(query, self.cfg.rag.top_k, self.cfg.rag.min_score)
        except LLMError as exc:
            log.warning("retrieval from %s failed: %s", collection, exc)
            return []

    # ---------------------------------------------------------------- audit
    def audit(self, **fields) -> None:
        line = json.dumps({"ts": time.time(), **fields}, default=str)
        log.info("audit %s", line)
        if self.audit_path:
            try:
                with open(self.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    # ---------------------------------------------------------------- planning
    async def model_is_warm(self) -> bool:
        return await self.llm.is_loaded(self.cfg.llm.chat_model)

    async def plan(self, text: str, request_id: str) -> Reply | Proposal:
        hits = await self.retrieve("admin", text)
        messages = [{"role": "system", "content": ADMIN_SYSTEM_PROMPT}]
        if hits:
            messages.append({"role": "system", "content": admin_notes_message(hits)})
        messages.append({"role": "user", "content": text})
        options = {"temperature": self.cfg.llm.temperature, **self.cfg.llm.options}
        started = time.monotonic()
        try:
            async with self.llm_lock:
                msg = await self.llm.chat(self.cfg.llm.chat_model, messages,
                                          [t.ollama_schema() for t in self.tools.values()], options)
        except LLMError as exc:
            self.audit(event="llm_error", request_id=request_id, error=str(exc))
            return Reply(f"⚠️ The model isn't responding: {exc}")
        latency = round(time.monotonic() - started, 2)
        notes = [f"{h.title} ({h.score:.2f})" for h in hits]

        calls = msg.get("tool_calls") or []
        if not calls:
            content = THINK_RE.sub("", msg.get("content") or "").split("</think>")[-1].strip()
            self.audit(event="plan", request_id=request_id, text=text, tool=None, notes=notes,
                       latency=latency)
            return Reply(content or "I'm not sure how to help with that.")

        fn = calls[0].get("function", {})
        name, args = fn.get("name"), fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        try:
            spec, clean = validate_call(self.tools, name, args)
        except ToolError as exc:
            self.audit(event="plan", request_id=request_id, text=text, tool=name, args=args,
                       outcome="invalid", error=str(exc), latency=latency)
            return Reply(f"🚫 I can't do that: {exc}.")

        # Ground the duration in the user's own words. Small models sometimes invent a
        # number (e.g. "a week" -> 16 h) instead of explaining the limit.
        if spec.name == "immich_queue" and clean.get("action") == "pause":
            asked = duration_hours_in(text)
            max_h = self.cfg.integrations.immich.max_pause_hours
            if asked is not None:
                if asked > max_h:
                    self.audit(event="plan", request_id=request_id, text=text, tool=name, args=clean,
                               outcome="over_limit", asked_hours=asked, latency=latency)
                    return Reply(f"🚫 I can pause for at most {max_h} hours. You asked for "
                                 f"{asked:g} hours; ask for {max_h} or less and pause again later if needed.")
                asked_int = max(1, round(asked))
                if clean.get("hours") != asked_int:
                    self.audit(event="corrected", request_id=request_id, field="hours",
                               model_value=clean.get("hours"), user_value=asked_int)
                    clean = {**clean, "hours": asked_int}

        self.audit(event="plan", request_id=request_id, text=text, tool=name, args=clean,
                   outcome="valid", extra_calls=len(calls) - 1, latency=latency)
        return Proposal(spec=spec, args=clean, request_id=request_id)

    # ---------------------------------------------------------------- execution
    async def execute(self, p: Proposal) -> str:
        status, body = await self.executor.run(p.verb, p.exec_args, p.request_id)
        self.audit(event="execute", request_id=p.request_id, verb=p.verb, args=p.exec_args,
                   http_status=status, ok=body.get("ok"))
        if status == 200 and body.get("ok"):
            if p.verb == "reload_config":
                try:
                    self.reload()
                except Exception as exc:  # executor accepted it; agent should too, but be safe
                    return f"Executor reloaded, but the agent couldn't: {exc}"
            return format_result(p.verb, body.get("result", {}), self.tz)
        return format_failure(status, body)

    # ---------------------------------------------------------------- help page
    async def answer_help(self, question: str, request_id: str) -> HelpAnswer:
        """Answer a help-page question from the help docs only. No tools are ever sent."""
        hcfg = self.cfg.help
        owner = hcfg.owner_name
        hits = await self.retrieve("help", question)
        sources = [{"title": h.title, "source": h.chunk.source, "score": round(h.score, 3)} for h in hits]

        if not hits:
            self.audit(event="help", request_id=request_id, question=question, outcome="no_docs")
            return HelpAnswer(self._forward_text(owner), [], hcfg.forward_unanswered, "no_docs")

        messages = [{"role": "system", "content": HELP_SYSTEM_PROMPT},
                    {"role": "user", "content": help_user_message(question, hits)}]
        options = {"temperature": 0.1, **self.cfg.llm.options, "num_predict": hcfg.max_answer_tokens}
        started = time.monotonic()
        try:
            async with self.llm_lock:
                msg = await self.llm.chat(self.cfg.llm.chat_model, messages, [], options)
        except LLMError as exc:
            self.audit(event="help", request_id=request_id, question=question, outcome="error", error=str(exc))
            return HelpAnswer(f"Sorry, the help assistant isn't available right now. Please contact {owner}.",
                              [], False, "error")
        latency = round(time.monotonic() - started, 2)
        answer = THINK_RE.sub("", msg.get("content") or "").split("</think>")[-1].strip()

        answer, cited = cited_sources(answer, hits)

        if not answer or answer.strip(" .*`\"'").upper().startswith("UNSURE"):
            self.audit(event="help", request_id=request_id, question=question, outcome="unsure",
                       sources=sources, latency=latency)
            return HelpAnswer(self._forward_text(owner), [], hcfg.forward_unanswered, "unsure")

        # Every web address must come from the docs it was given. Small models sometimes
        # garble one ("jellyin" for "jellyfin"); near-misses are corrected, anything else
        # means the answer is not shown.
        answer, corrections, grounded = ground_urls(answer, "\n".join(h.text for h in hits))
        if corrections:
            self.audit(event="corrected", request_id=request_id, field="url",
                       changes=[{"model": a, "docs": b} for a, b in corrections])
        if not grounded:
            self.audit(event="help", request_id=request_id, question=question, outcome="ungrounded_url",
                       sources=sources, latency=latency)
            return HelpAnswer(self._forward_text(owner), [], hcfg.forward_unanswered, "unsure")

        used = [{"title": h.title, "source": h.chunk.source, "score": round(h.score, 3)} for h in cited]
        self.audit(event="help", request_id=request_id, question=question, outcome="answered",
                   sources=sources, cited=[u["title"] for u in used], latency=latency)
        return HelpAnswer(answer, used, False, "answered")

    def _forward_text(self, owner: str) -> str:
        if self.cfg.help.forward_unanswered:
            return f"I couldn't find an answer to that in the help pages, so I've passed your question to {owner}."
        return f"I couldn't find an answer to that in the help pages. Please contact {owner}."
