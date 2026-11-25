"""Minimal Immich job-queue client. The API key is read from the environment and never
leaves the executor container."""

from __future__ import annotations

import httpx


class ImmichError(Exception):
    pass


class ImmichClient:
    def __init__(self, url: str, api_key: str, api_style: str = "jobs", timeout: float = 15.0):
        if not api_key:
            raise ImmichError("IMMICH_API_KEY is not set")
        self.url = url.rstrip("/")
        self.api_style = api_style
        self._client = httpx.Client(
            timeout=timeout,
            headers={"x-api-key": api_key, "Accept": "application/json"},
        )

    def queue_command(self, queue: str, command: str) -> dict:
        if command not in ("pause", "resume"):
            raise ImmichError(f"unsupported command {command!r}")
        if self.api_style != "jobs":
            raise ImmichError(f"unsupported api_style {self.api_style!r}")
        resp = self._client.put(f"{self.url}/api/jobs/{queue}", json={"command": command, "force": False})
        if resp.status_code >= 400:
            raise ImmichError(f"Immich returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        status = body.get("queueStatus", {}) if isinstance(body, dict) else {}
        return {"queue": queue, "command": command, "is_paused": status.get("isPaused")}
