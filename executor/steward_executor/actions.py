"""The verb table. This file is the whole of what Server Steward can do to a host.

Every request is validated here against steward.yaml, even though the agent validated it
too. The agent faces Discord and an LLM; this code assumes its input may be hostile.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import readers
from .config import ExecutorConfig
from .hostinfo import host_info
from .schedules import Schedule, ScheduleStore


class Rejected(Exception):
    """Input failed validation. Nothing was done."""


class ActionFailed(Exception):
    """Validation passed but the action failed part-way. `partial` says what happened."""

    def __init__(self, message: str, partial: Any = None):
        super().__init__(message)
        self.partial = partial


VERBS = ("status", "restart_stack", "immich_queue", "list_schedules", "reload_config",
         # read-only integrations
         "backup_status", "vpn_status", "storage_usage", "torrent_list", "streams",
         "immich_jobs", "container_logs")


def _require_args(args: Any, allowed: set[str], required: set[str]) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise Rejected("args must be an object")
    unknown = set(args) - allowed
    if unknown:
        raise Rejected(f"unknown argument(s): {', '.join(sorted(unknown))}")
    missing = required - set(args)
    if missing:
        raise Rejected(f"missing argument(s): {', '.join(sorted(missing))}")
    return args


class Executor:
    def __init__(
        self,
        cfg: ExecutorConfig,
        docker_client: Any,
        schedules: ScheduleStore,
        immich_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        host_root: str = "/",
        config_loader: Callable[[], ExecutorConfig] | None = None,
        http: Any = None,
        secrets: dict | None = None,
    ):
        self.cfg = cfg
        self._config_loader = config_loader
        self.http = http                      # httpx.Client for read-only API calls
        self.secrets = secrets or {}          # IMMICH_API_KEY, JELLYFIN_API_KEY, TRANSMISSION_*
        self.docker = docker_client
        self.schedules = schedules
        self._immich_factory = immich_factory
        self._immich = None
        self.clock = clock
        self.sleep = sleep
        self.host_root = host_root
        # One mutating action at a time. Status calls don't take the lock.
        self._mutate = threading.Lock()

    # ------------------------------------------------------------------ dispatch
    def run(self, verb: Any, args: Any) -> dict:
        if verb not in VERBS:
            raise Rejected(f"unknown verb {verb!r}")
        handler = getattr(self, f"_v_{verb}")
        return handler(args)

    # ------------------------------------------------------------------ verbs
    def _v_status(self, args: Any) -> dict:
        _require_args(args, set(), set())
        containers = {name: self._container_state(name) for name in self.cfg.watched_containers()}
        return {
            "containers": containers,
            "host": host_info(self.cfg.status.disks, root=self.host_root),
        }

    def _v_list_schedules(self, args: Any) -> dict:
        _require_args(args, set(), set())
        return {
            "schedules": [
                {"verb": s.verb, "args": s.args, "run_at": s.run_at, "attempts": s.attempts}
                for s in self.schedules.list()
            ]
        }

    def _v_reload_config(self, args: Any) -> dict:
        """Re-read steward.yaml. Only the owner can edit that file, so this can't widen
        what the agent may do beyond what the owner wrote. A bad file keeps the old config."""
        _require_args(args, set(), set())
        if self._config_loader is None:
            raise Rejected("reload is not available")
        try:
            new_cfg = self._config_loader()
        except Exception as exc:
            raise ActionFailed(f"config not reloaded, keeping the previous one: {exc}")
        if not self._mutate.acquire(timeout=1):
            raise Rejected("another action is already running")
        try:
            old_immich = self.cfg.integrations.immich
            self.cfg = new_cfg
            if new_cfg.integrations.immich != old_immich:
                self._immich = None  # rebuild the client with the new URL/settings
        finally:
            self._mutate.release()
        return {
            "stacks": sorted(new_cfg.stacks),
            "immich_enabled": new_cfg.integrations.immich.enabled,
        }

    def _v_restart_stack(self, args: Any) -> dict:
        args = _require_args(args, {"name"}, {"name"})
        name = args["name"]
        if not isinstance(name, str) or name not in self.cfg.stacks:
            raise Rejected(f"unknown stack {name!r}; allowed: {', '.join(sorted(self.cfg.stacks))}")
        stack = self.cfg.stacks[name]

        if not self._mutate.acquire(timeout=1):
            raise Rejected("another action is already running")
        try:
            steps: list[dict] = []
            for step in stack.restart_sequence:
                entry = {"container": step.container}
                attempts = 0
                while True:
                    attempts += 1
                    try:
                        container = self.docker.containers.get(step.container)
                        container.restart(timeout=30)
                    except Exception as exc:  # docker.errors.NotFound / APIError
                        entry["result"] = f"error: {exc}"
                        steps.append(entry)
                        raise ActionFailed(f"restart of {step.container} failed", {"stack": name, "steps": steps})
                    if not step.wait_healthy:
                        entry["result"] = "restarted"
                        break
                    state = self._wait_ready(container, step.timeout_seconds)
                    entry["result"] = state
                    if state in ("healthy", "running"):
                        break
                    if attempts > step.retries:
                        entry["attempts"] = attempts
                        steps.append(entry)
                        raise ActionFailed(
                            f"{step.container} not healthy after {attempts} attempt(s) of "
                            f"{step.timeout_seconds}s; later steps skipped",
                            {"stack": name, "steps": steps},
                        )
                if attempts > 1:
                    entry["attempts"] = attempts
                steps.append(entry)
            return {"stack": name, "steps": steps}
        finally:
            self._mutate.release()

    def _v_immich_queue(self, args: Any) -> dict:
        icfg = self.cfg.integrations.immich
        if not icfg.enabled:
            raise Rejected("the Immich integration is not enabled")
        args = _require_args(args, {"action", "queue", "hours"}, {"action", "queue"})
        action, queue, hours = args["action"], args["queue"], args.get("hours")
        if action not in ("pause", "resume"):
            raise Rejected("action must be 'pause' or 'resume'")
        if queue not in icfg.queues:
            raise Rejected(f"unknown queue {queue!r}; allowed: {', '.join(icfg.queues)}")
        if hours is not None:
            if action != "pause":
                raise Rejected("hours is only valid with action 'pause'")
            if isinstance(hours, bool) or not isinstance(hours, (int, float)):
                raise Rejected("hours must be a number")
            if not 1 <= hours <= icfg.max_pause_hours:
                raise Rejected(f"hours must be between 1 and {icfg.max_pause_hours}")

        if not self._mutate.acquire(timeout=1):
            raise Rejected("another action is already running")
        try:
            result = self._immich_client().queue_command(queue, action)
            key = f"immich_resume:{queue}"
            if action == "pause" and hours is not None:
                run_at = self.clock() + float(hours) * 3600
                self.schedules.put(Schedule(key=key, verb="immich_queue",
                                            args={"action": "resume", "queue": queue}, run_at=run_at))
                result["resume_at"] = run_at
            elif action == "resume":
                result["cancelled_schedule"] = self.schedules.cancel(key)
            return result
        finally:
            self._mutate.release()

    # ------------------------------------------------------------------ read-only integrations
    def _integration(self, name: str):
        icfg = getattr(self.cfg.integrations, name)
        if not icfg.enabled:
            raise Rejected(f"the {name.replace('_', ' ')} integration is not enabled")
        return icfg

    def _read(self, fn, *args):
        try:
            return fn(*args)
        except readers.ReaderError as exc:
            raise ActionFailed(str(exc))
        except readers.UnknownUser as exc:
            raise Rejected(f"no user called {exc.args[0]!r}")
        except Exception as exc:  # network errors etc.: report, don't crash
            raise ActionFailed(f"couldn't read it: {type(exc).__name__}: {exc}"[:300])

    def _v_backup_status(self, args: Any) -> dict:
        _require_args(args, set(), set())
        return self._read(readers.backup_status, self._integration("backup_log"))

    def _v_vpn_status(self, args: Any) -> dict:
        _require_args(args, set(), set())
        vcfg = self._integration("vpn")
        try:
            container = self.docker.containers.get(vcfg.container)
        except Exception:
            raise ActionFailed(f"container {vcfg.container!r} not found")
        return self._read(readers.vpn_status, container)

    def _v_storage_usage(self, args: Any) -> dict:
        args = _require_args(args, {"app", "user"}, {"app"})
        app, user = args["app"], args.get("user")
        if user is not None and (not isinstance(user, str) or not readers.USER_RE.match(user)):
            raise Rejected("user must be a plain user name")
        if app == "immich":
            icfg = self._integration("immich")
            return self._read(readers.immich_storage, self._immich_http(), icfg.url, user)
        if app == "nextcloud":
            ncfg = self._integration("nextcloud")
            try:
                container = self.docker.containers.get(ncfg.container)
            except Exception:
                raise ActionFailed(f"container {ncfg.container!r} not found")
            return self._read(readers.nextcloud_storage, container, user)
        raise Rejected("app must be 'immich' or 'nextcloud'")

    def _v_torrent_list(self, args: Any) -> dict:
        _require_args(args, set(), set())
        tcfg = self._integration("transmission")
        rpc = readers.TransmissionRPC(self.http, tcfg.url, self.secrets.get("TRANSMISSION_USER", ""),
                                      self.secrets.get("TRANSMISSION_PASS", ""))
        return self._read(readers.torrent_list, rpc)

    def _v_streams(self, args: Any) -> dict:
        """Everyone watching or listening, across every enabled media app. One app being
        down doesn't hide the others; its error is reported alongside."""
        _require_args(args, set(), set())
        sources = []
        ig = self.cfg.integrations
        if ig.jellyfin.enabled:
            sources.append(("Jellyfin", readers.jellyfin_streams, ig.jellyfin.url, "JELLYFIN_API_KEY"))
        if ig.audiobookshelf.enabled:
            sources.append(("Audiobookshelf", readers.audiobookshelf_streams, ig.audiobookshelf.url,
                            "AUDIOBOOKSHELF_TOKEN"))
        if not sources:
            raise Rejected("no streaming integration (jellyfin, audiobookshelf) is enabled")
        streams, errors = [], []
        for label, fn, url, secret in sources:
            try:
                streams.extend(self._read(fn, self.http, url, self.secrets.get(secret, ""))["streams"])
            except ActionFailed as exc:
                errors.append(f"{label}: {exc}")
        if errors and not streams and len(errors) == len(sources):
            raise ActionFailed("; ".join(errors))
        return {"streams": streams, "errors": errors, "apps": [s[0] for s in sources]}

    def _v_immich_jobs(self, args: Any) -> dict:
        _require_args(args, set(), set())
        icfg = self._integration("immich")
        return self._read(readers.immich_jobs, self._immich_http(), icfg.url)

    def _v_container_logs(self, args: Any) -> dict:
        args = _require_args(args, {"container", "lines"}, {"container"})
        name, lines = args["container"], args.get("lines", 30)
        allowed = self.cfg.watched_containers()
        if name not in allowed:
            raise Rejected(f"unknown container {name!r}; allowed: {', '.join(allowed)}")
        if isinstance(lines, bool) or not isinstance(lines, int) or not 5 <= lines <= 50:
            raise Rejected("lines must be a whole number from 5 to 50")
        try:
            container = self.docker.containers.get(name)
        except Exception:
            raise ActionFailed(f"container {name!r} not found")
        return self._read(readers.container_logs, container, lines)

    def _immich_http(self):
        """The shared http client with the Immich key header added per request."""
        key = self.secrets.get("IMMICH_API_KEY", "")
        if not key:
            raise ActionFailed("IMMICH_API_KEY is not set")
        http = self.http

        class _WithKey:
            def get(self, url, **kw):
                headers = {**kw.pop("headers", {}), "x-api-key": key, "Accept": "application/json"}
                return http.get(url, headers=headers, **kw)
        return _WithKey()

    # ------------------------------------------------------------------ helpers
    def _immich_client(self):
        if self._immich is None:
            if self._immich_factory is None:
                raise ActionFailed("Immich client not configured")
            self._immich = self._immich_factory()
        return self._immich

    def _container_state(self, name: str) -> dict:
        try:
            c = self.docker.containers.get(name)
        except Exception:
            return {"state": "missing"}
        state = c.attrs.get("State", {})
        out = {"state": state.get("Status", "unknown")}
        health = state.get("Health", {}).get("Status")
        if health:
            out["health"] = health
        return out

    def _wait_ready(self, container: Any, timeout: int) -> str:
        """Wait for Docker health 'healthy', or 'running' if there is no healthcheck."""
        deadline = self.clock() + timeout
        last = "unknown"
        while True:
            container.reload()
            state = container.attrs.get("State", {})
            health = state.get("Health", {}).get("Status")
            last = health or state.get("Status", "unknown")
            if health == "healthy" or (health is None and last == "running"):
                return last
            if self.clock() >= deadline:
                return last
            self.sleep(2)

    # ------------------------------------------------------------------ scheduler
    def run_due_schedules(self, audit: Callable[..., None]) -> None:
        for s in self.schedules.due(self.clock()):
            try:
                result = self.run(s.verb, s.args)
                self.schedules.cancel(s.key)
                audit(source="schedule", verb=s.verb, args=s.args, outcome="ok", result=result)
            except Exception as exc:
                kept = self.schedules.record_failure(s.key)
                audit(source="schedule", verb=s.verb, args=s.args, outcome="error",
                      error=str(exc), will_retry=kept)
