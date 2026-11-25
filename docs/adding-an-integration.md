# Adding an integration

An integration is five small pieces, split between the two containers so the model never gets closer to your apps than it does today. This walks through a read-only one: "is my monitoring dashboard showing anything down?", for a made-up app called **ExampleMonitor** with an HTTP endpoint `GET /api/summary` that returns `{"up": 12, "down": ["backups"]}`.

| # | Where | What |
| --- | --- | --- |
| 1 | `executor/steward_executor/config.py` | A strict config block |
| 2 | `executor/steward_executor/readers.py` | A function that reads the app |
| 3 | `executor/steward_executor/actions.py` | A verb that validates input and calls the reader |
| 4 | `steward/steward_agent/` (`config.py`, `tools.py`) | The tool or topic the model sees |
| 5 | `steward/steward_agent/formatting.py` | How the result reads in Discord |

Plus tests for each side, an `.env` entry if it needs a secret, and a network in `compose.local.example.yaml`.

## 1. Config (executor)

Executor config is strict (`extra="forbid"`), so typos fail at startup instead of silently changing behaviour.

```python
class ExampleMonitorConfig(Strict):
    """Monitoring summary. API key from EXAMPLEMONITOR_API_KEY."""
    enabled: bool = False
    url: str = "http://examplemonitor:3000"


class Integrations(Strict):
    ...
    examplemonitor: ExampleMonitorConfig = Field(default_factory=ExampleMonitorConfig)
```

## 2. Reader (executor)

Readers only read, take their dependencies as arguments (so tests can fake them), and raise `ReaderError` with a message that's safe to show you.

```python
def monitor_summary(http: httpx.Client, url: str, api_key: str) -> dict:
    if not api_key:
        raise ReaderError("EXAMPLEMONITOR_API_KEY is not set")
    resp = http.get(f"{url.rstrip('/')}/api/summary", headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code >= 400:
        raise ReaderError(f"ExampleMonitor returned HTTP {resp.status_code}")
    data = resp.json()
    return {"up": int(data.get("up", 0)), "down": [str(n)[:80] for n in data.get("down", [])][:50]}
```

Return plain data and cap sizes: this goes back through the agent to Discord.

## 3. Verb (executor)

Add the verb name to `VERBS` and a `_v_<verb>` method. Every argument is checked here even though the agent already checked it; treat input as hostile.

```python
VERBS = (..., "monitor_summary")

    def _v_monitor_summary(self, args: Any) -> dict:
        _require_args(args, set(), set())                 # allowed, required
        wcfg = self._integration("examplemonitor")            # Rejected if not enabled
        return self._read(readers.monitor_summary, self.http, wcfg.url,
                          self.secrets.get("EXAMPLEMONITOR_API_KEY", ""))
```

`_read` turns reader errors into a clean failure message. For a verb that **changes** something:

- take `self._mutate` (one change at a time; see `_v_restart_stack`),
- validate every argument against the config: names from an allowlist, numbers within limits,
- give it `risk="confirm"` in the agent (step 4) so it needs a ✅.

Secrets: add the variable to `secrets` in `executor/steward_executor/main.py`, to the executor's `environment:` in `compose.yaml`, and to `.env.example`. Never give it to the `steward` service.

## 4. Tool (agent)

The agent's config is lenient; it only needs to know whether the integration is on:

```python
class Integrations(Loose):
    ...
    examplemonitor: Toggle = Field(default_factory=Toggle)
```

Then offer it to the model in `build_tools()` in `tools.py`. Prefer adding a **topic to an existing tool** over a new tool: small models choose worse as the tool list grows. A read-only status fits `get_status`:

```python
    if integ.examplemonitor.enabled:
        topics["monitoring"] = ("monitor_summary", "which monitored services are down")
```

A new tool looks like this (see `storage_usage` or `immich_queue` for full examples):

```python
        tools["examplemonitor"] = ToolSpec(
            name="examplemonitor",
            description="Show which monitored services are up or down (read only).",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            risk="read",                     # "confirm" for anything that changes state
            verb="monitor_summary",
            describe=lambda a: "check monitoring",
        )
```

Descriptions are written for the model: say what it does and when to use it, in a sentence. Use `enum` for every argument that has a fixed set of values, filled from the config, so the model can only pick real names. Use `route=` when one tool maps to several verbs.

## 5. Formatting (agent)

Results are turned into text by code, never by the model, so it can't misreport what happened.

```python
def format_monitor(r: dict) -> str:
    if not r["down"]:
        return f"✅ All {r['up']} monitored services are up."
    return f"⚠️ {len(r['down'])} down: " + ", ".join(r["down"]) + f" ({r['up']} up)"


def format_result(verb, result, tz):
    ...
    if verb == "monitor_summary":
        return format_monitor(result)
```

Keep messages under Discord's 2000-character limit.

## Tests

Both suites run without Docker, Discord or a model:

```bash
cd executor && pip install -r requirements.txt pytest && python -m pytest -q
cd ../steward && pip install -r requirements.txt pytest && python -m pytest -q
```

- **Executor:** use `httpx.MockTransport` for HTTP apps and a fake container for Docker-based ones (see `executor/tests/test_readers.py`). Test the happy path, the app being down, a bad credential, and every rejected input.
- **Agent:** check the tool appears only when enabled, that arguments route to the right verb, and the formatted text (see `steward/tests/test_agent.py`).

## Wiring it up

1. `compose.local.example.yaml`: add the app's network, commented.
2. `config/steward.example.yaml`: add the block, `enabled: false`, with a comment saying what it needs.
3. `docs/integrations.md`: a section with setup, network, credential, verb, and how to test it.
4. Restart: `docker compose up -d --build`, then test with the `act` helper in [integrations.md](integrations.md#testing-an-integration-without-discord) before trying it in Discord.
