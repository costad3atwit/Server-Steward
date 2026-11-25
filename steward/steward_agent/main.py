"""Entry point for the steward container."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import Agent
from .bot import StewardBot
from .help_api import create_help_app
from .clients import ExecutorClient, OllamaClient
from .config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("discord").setLevel(logging.WARNING)
log = logging.getLogger("steward")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set (see .env.example)")
    return value


def main() -> None:
    token = _require("DISCORD_BOT_TOKEN")
    try:
        owners = {int(x) for x in _require("DISCORD_OWNER_IDS").replace(" ", "").split(",") if x}
    except ValueError:
        sys.exit("DISCORD_OWNER_IDS must be comma-separated numeric Discord user IDs")
    exec_token = _require("EXECUTOR_TOKEN")
    config_path = os.environ.get("STEWARD_CONFIG", "/config/steward.yaml")
    data = Path(os.environ.get("STEWARD_DATA", "/data"))

    try:
        tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    except Exception:
        tz = ZoneInfo("UTC")

    audit_path = data / "audit.log"
    if not os.access(data, os.W_OK):
        log.warning("%s is not writable; audit goes to container logs only "
                    "(fix: sudo chown 10001:10001 <data dir>/steward)", data)
        audit_path = None

    docs_dir = Path(os.environ.get("STEWARD_DOCS", "/docs"))
    index_dir = data / "index" if os.access(data, os.W_OK) else Path("/tmp/steward-index")

    agent = Agent(
        cfg_loader=lambda: load_config(config_path),
        llm=OllamaClient(os.environ.get("OLLAMA_URL", "http://ollama:11434")),
        executor=ExecutorClient(os.environ.get("EXECUTOR_URL", "http://steward-executor:8080"), exec_token),
        tz=tz,
        audit_path=audit_path,
        docs_dir=docs_dir,
        index_dir=index_dir,
    )
    log.info("tools: %s", ", ".join(agent.tools))
    asyncio.run(serve(agent, owners, token))


async def refresh_loop(agent: Agent, every: int = 60) -> None:
    """Index docs at startup, then pick up edits to the docs folders within a minute."""
    while True:
        await agent.refresh_indexes()
        await asyncio.sleep(every)


async def serve(agent: Agent, owners: set[int], token: str) -> None:
    import uvicorn

    bot = StewardBot(agent, owners)
    tasks = [bot.start(token), refresh_loop(agent)]
    if agent.cfg.help.enabled:
        app = create_help_app(agent, forward=bot.dm_owners)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8088,
                                               log_level="warning", access_log=False))
        tasks.append(server.serve())
        log.info("help endpoint on :8088 (POST /help)")
    async with bot:
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    main()
