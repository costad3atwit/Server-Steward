"""Retrieval and help checks without Discord. Run inside the container:

    docker compose exec steward python -m steward_agent.cli search help "how do I add a device"
    docker compose exec steward python -m steward_agent.cli ask "how do I add a device"
    docker compose exec steward python -m steward_agent.cli chunks help

`search` prints every chunk score (including ones below min_score) so you can tune
rag.min_score. `ask` runs the full help path but never forwards to Discord.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import Agent
from .clients import ExecutorClient, OllamaClient
from .config import load_config


def build_agent() -> Agent:
    data = Path(os.environ.get("STEWARD_DATA", "/data"))
    return Agent(
        cfg_loader=lambda: load_config(os.environ.get("STEWARD_CONFIG", "/config/steward.yaml")),
        llm=OllamaClient(os.environ.get("OLLAMA_URL", "http://ollama:11434")),
        executor=ExecutorClient(os.environ.get("EXECUTOR_URL", "http://steward-executor:8080"),
                                os.environ.get("EXECUTOR_TOKEN", "")),
        tz=ZoneInfo(os.environ.get("TZ", "UTC")),
        docs_dir=Path(os.environ.get("STEWARD_DOCS", "/docs")),
        index_dir=data / "index" if os.access(data, os.W_OK) else Path("/tmp/steward-index"),
    )


async def run(args) -> None:
    agent = build_agent()
    await agent.refresh_indexes()
    if args.cmd == "chunks":
        idx = agent.indexes[args.collection]
        for c in idx._chunks:
            print(f"- {c.title}  [{c.source}, {len(c.text)} chars]")
        print(f"{idx.size} chunks")
    elif args.cmd == "search":
        idx = agent.indexes[args.collection]
        hits = await idx.search(args.query, top_k=args.k, min_score=0.0)
        cut = agent.cfg.rag.min_score
        for h in hits:
            mark = "✔" if h.score >= cut else "✘"
            print(f"{mark} {h.score:.3f}  {h.title}  [{h.chunk.source}]")
        print(f"(min_score = {cut}; ✘ rows would not be used)")
    elif args.cmd == "ask":
        res = await agent.answer_help(args.question, "cli")
        print(res.answer)
        print(f"\n[{res.reason}] forwarded={res.forwarded}")
        for s in res.sources:
            print(f"  source: {s['title']} ({s['score']})")


def main() -> None:
    ap = argparse.ArgumentParser(prog="steward-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("collection", choices=["admin", "help"])
    s.add_argument("query"); s.add_argument("-k", type=int, default=8)
    a = sub.add_parser("ask"); a.add_argument("question")
    c = sub.add_parser("chunks"); c.add_argument("collection", choices=["admin", "help"])
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
