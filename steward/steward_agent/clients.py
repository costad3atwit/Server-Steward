"""HTTP clients for Ollama and the executor."""

from __future__ import annotations

import httpx


class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 180.0):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def chat(self, model: str, messages: list[dict], tools: list[dict],
                   options: dict | None = None) -> dict:
        body = {"model": model, "messages": messages, "stream": False, "options": options or {}}
        if tools:
            body["tools"] = tools
        try:
            resp = await self._http.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"could not reach the model server: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"model server returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("message", {}) or {}

    async def embed(self, model: str, inputs: list[str], options: dict | None = None) -> list[list[float]]:
        body = {"model": model, "input": inputs, "options": options or {}}
        try:
            resp = await self._http.post("/api/embed", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"could not reach the model server: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"embedding failed, HTTP {resp.status_code}: {resp.text[:200]}")
        vectors = resp.json().get("embeddings") or []
        if len(vectors) != len(inputs):
            raise LLMError("embedding response had the wrong number of vectors")
        return vectors

    async def is_loaded(self, model: str) -> bool:
        try:
            resp = await self._http.get("/api/ps", timeout=5)
            names = {m.get("name") for m in resp.json().get("models", [])}
            return model in names
        except Exception:
            return False


class ExecutorClient:
    def __init__(self, base_url: str, token: str, timeout: float = 900.0):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def run(self, verb: str, args: dict, request_id: str = "") -> tuple[int, dict]:
        """Returns (http_status, body). Never raises for HTTP errors."""
        try:
            resp = await self._http.post("/v1/action", json={"verb": verb, "args": args},
                                         headers={"X-Request-ID": request_id[:64]})
        except httpx.HTTPError as exc:
            return 0, {"ok": False, "error": "unreachable", "detail": str(exc)}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"ok": False, "error": "bad response", "detail": resp.text[:200]}
