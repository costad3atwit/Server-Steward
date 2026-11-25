"""HTTP front door for the executor. Only reachable on the internal Docker network."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .actions import ActionFailed, Executor, Rejected
from .config import load_config
from .schedules import ScheduleStore

CONFIG_PATH = os.environ.get("STEWARD_CONFIG", "/config/steward.yaml")
DATA_DIR = Path(os.environ.get("STEWARD_DATA", "/data"))
TOKEN = os.environ.get("EXECUTOR_TOKEN", "")
SCHEDULE_POLL_SECONDS = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("steward.executor")


class ActionRequest(BaseModel):
    verb: str
    args: dict = {}


def audit(**fields: Any) -> None:
    record = {"ts": time.time(), **fields}
    line = json.dumps(record, default=str)
    log.info("audit %s", line)
    try:
        with open(DATA_DIR / "audit.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        log.error("could not write audit log: %s", exc)


def build_executor() -> Executor:
    import docker

    from .immich import ImmichClient

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("IMMICH_API_KEY", "")
    executor: Executor

    def immich_factory():
        icfg = executor.cfg.integrations.immich  # always the current config
        return ImmichClient(icfg.url, key, icfg.api_style)

    import httpx

    secrets = {k: os.environ.get(k, "") for k in
               ("IMMICH_API_KEY", "JELLYFIN_API_KEY", "AUDIOBOOKSHELF_TOKEN",
                "TRANSMISSION_USER", "TRANSMISSION_PASS")}
    executor = Executor(
        cfg=load_config(CONFIG_PATH),
        docker_client=docker.from_env(),
        schedules=ScheduleStore(DATA_DIR / "schedules.json"),
        immich_factory=immich_factory,
        config_loader=lambda: load_config(CONFIG_PATH),
        http=httpx.Client(timeout=20.0),
        secrets=secrets,
    )
    return executor


def create_app(executor: Executor | None = None, token: str | None = None) -> FastAPI:
    token = TOKEN if token is None else token
    if len(token) < 32:
        raise SystemExit("EXECUTOR_TOKEN must be set and at least 32 characters (openssl rand -hex 32)")

    state: dict[str, Any] = {"executor": executor}

    async def scheduler_loop() -> None:
        while True:
            try:
                await run_in_threadpool(state["executor"].run_due_schedules, audit)
            except Exception:
                log.exception("scheduler loop error")
            await asyncio.sleep(SCHEDULE_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["executor"] is None:
            state["executor"] = build_executor()
        task = asyncio.create_task(scheduler_loop())
        log.info("executor ready")
        yield
        task.cancel()

    app = FastAPI(title="Server Steward executor", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    def check_auth(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/action")
    def action(body: ActionRequest, request: Request,
               authorization: str | None = Header(default=None),
               x_request_id: str | None = Header(default=None)):
        try:
            check_auth(authorization)
        except HTTPException:
            audit(source="api", verb=body.verb, outcome="unauthorized",
                  client=request.client.host if request.client else None)
            raise
        rid = (x_request_id or "")[:64]
        try:
            result = state["executor"].run(body.verb, body.args)
        except Rejected as exc:
            audit(source="api", request_id=rid, verb=body.verb, args=body.args, outcome="rejected", error=str(exc))
            return JSONResponse(status_code=400, content={"ok": False, "error": "rejected", "detail": str(exc)})
        except ActionFailed as exc:
            audit(source="api", request_id=rid, verb=body.verb, args=body.args, outcome="failed",
                  error=str(exc), partial=exc.partial)
            return JSONResponse(status_code=500, content={"ok": False, "error": "failed",
                                                          "detail": str(exc), "partial": exc.partial})
        except Exception as exc:
            log.exception("unexpected error")
            audit(source="api", request_id=rid, verb=body.verb, args=body.args, outcome="error", error=str(exc))
            return JSONResponse(status_code=500, content={"ok": False, "error": "error", "detail": str(exc)})
        audit(source="api", request_id=rid, verb=body.verb, args=body.args, outcome="ok", result=result)
        return {"ok": True, "verb": body.verb, "result": result}

    return app


def run() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8080, log_level="info", access_log=False)


if __name__ == "__main__":
    run()
