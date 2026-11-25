import pytest
from fastapi.testclient import TestClient

from steward_executor import main as main_mod
from steward_executor.actions import ActionFailed, Executor, Rejected
from steward_executor.config import ExecutorConfig
from steward_executor.schedules import ScheduleStore

TOKEN = "t" * 64

CFG = {
    "llm": {"chat_model": "ignored-by-executor"},
    "stacks": {
        "torrent": {"restart_sequence": [
            {"container": "gluetun", "wait_healthy": True, "timeout_seconds": 10},
            {"container": "transmission"},
        ]},
        "jellyfin": {"restart_sequence": [{"container": "jellyfin"}]},
    },
    "status": {"extra_containers": ["caddy"]},
    "integrations": {"immich": {"enabled": True, "queues": ["videoConversion"], "max_pause_hours": 48}},
}


class FakeContainer:
    def __init__(self, name, health_sequence=None):
        self.name = name
        self.restarts = 0
        self._health = list(health_sequence or [])
        self.attrs = {"State": {"Status": "running"}}
        if self._health:
            self.attrs["State"]["Health"] = {"Status": "starting"}

    def restart(self, timeout=10):
        self.restarts += 1

    def reload(self):
        if self._health:
            self.attrs["State"]["Health"]["Status"] = self._health.pop(0)


class FakeDocker:
    def __init__(self, containers):
        self._c = {c.name: c for c in containers}
        self.containers = self

    def get(self, name):
        if name not in self._c:
            raise LookupError(f"No such container: {name}")
        return self._c[name]


class FakeImmich:
    def __init__(self):
        self.calls = []

    def queue_command(self, queue, command):
        self.calls.append((queue, command))
        return {"queue": queue, "command": command, "is_paused": command == "pause"}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    gluetun = FakeContainer("gluetun", ["starting", "healthy"])
    transmission = FakeContainer("transmission")
    jellyfin = FakeContainer("jellyfin")
    docker = FakeDocker([gluetun, transmission, jellyfin])
    immich = FakeImmich()
    ex = Executor(
        cfg=ExecutorConfig.model_validate(CFG),
        docker_client=docker,
        schedules=ScheduleStore(tmp_path / "schedules.json"),
        immich_factory=lambda: immich,
        clock=clock,
        sleep=clock.sleep,
        host_root=str(tmp_path),
    )
    return ex, docker, immich, clock, tmp_path


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("verb,args", [
    ("reboot_server", {}),
    ("restart_stack", {"name": "immich"}),            # not configured
    ("restart_stack", {"name": "../etc"}),
    ("restart_stack", {}),
    ("restart_stack", {"name": "torrent", "force": True}),
    ("restart_stack", "torrent"),
    ("immich_queue", {"action": "delete", "queue": "videoConversion"}),
    ("immich_queue", {"action": "pause", "queue": "library"}),
    ("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 168}),
    ("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 0}),
    ("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": True}),
    ("immich_queue", {"action": "resume", "queue": "videoConversion", "hours": 2}),
    ("status", {"verbose": True}),
])
def test_rejects_bad_input(env, verb, args):
    ex, docker, immich, *_ = env
    with pytest.raises(Rejected):
        ex.run(verb, args)
    assert all(c.restarts == 0 for c in docker._c.values())
    assert immich.calls == []


def test_config_rejects_typos():
    bad = {"stacks": {"x": {"restart_sequence": [{"container": "a", "wait_healty": True}]}}}
    with pytest.raises(Exception):
        ExecutorConfig.model_validate(bad)


# ---------------------------------------------------------------- restart
def test_restart_torrent_waits_for_gluetun(env):
    ex, docker, *_ = env
    out = ex.run("restart_stack", {"name": "torrent"})
    assert [s["container"] for s in out["steps"]] == ["gluetun", "transmission"]
    assert out["steps"][0]["result"] == "healthy"
    assert docker.get("gluetun").restarts == 1
    assert docker.get("transmission").restarts == 1


def test_restart_aborts_if_vpn_never_healthy(env):
    ex, docker, *_ = env
    docker.get("gluetun")._health = ["unhealthy"] * 100
    with pytest.raises(ActionFailed) as info:
        ex.run("restart_stack", {"name": "torrent"})
    assert docker.get("transmission").restarts == 0
    assert info.value.partial["steps"][0]["result"] == "unhealthy"


# ---------------------------------------------------------------- immich + schedules
def test_pause_with_hours_schedules_resume(env):
    ex, _, immich, clock, _ = env
    out = ex.run("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 24})
    assert immich.calls == [("videoConversion", "pause")]
    assert out["resume_at"] == clock.t + 24 * 3600
    assert len(ex.run("list_schedules", {})["schedules"]) == 1

    events = []
    ex.run_due_schedules(lambda **kw: events.append(kw))
    assert events == []                                  # not due yet
    clock.t += 24 * 3600
    ex.run_due_schedules(lambda **kw: events.append(kw))
    assert immich.calls[-1] == ("videoConversion", "resume")
    assert events[0]["outcome"] == "ok"
    assert ex.run("list_schedules", {})["schedules"] == []


def test_repause_replaces_schedule_and_survives_restart(env):
    ex, _, _, clock, tmp = env
    ex.run("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 2})
    ex.run("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 10})
    reloaded = ScheduleStore(tmp / "schedules.json").list()
    assert len(reloaded) == 1 and reloaded[0].run_at == clock.t + 10 * 3600


def test_manual_resume_cancels_schedule(env):
    ex, *_ = env
    ex.run("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 5})
    out = ex.run("immich_queue", {"action": "resume", "queue": "videoConversion"})
    assert out["cancelled_schedule"] is True


# ---------------------------------------------------------------- status
def test_status_reports_containers_and_host(env):
    ex, _, _, _, tmp = env
    (tmp / "proc").mkdir()
    (tmp / "proc/uptime").write_text("7200.5 100.0\n")
    (tmp / "proc/loadavg").write_text("0.50 0.40 0.30 1/200 999\n")
    hw = tmp / "sys/class/hwmon/hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("k10temp\n")
    (hw / "temp1_input").write_text("67500\n")
    out = ex.run("status", {})
    assert out["containers"]["gluetun"]["state"] == "running"
    assert out["containers"]["caddy"] == {"state": "missing"}
    assert out["host"]["uptime_hours"] == 2.0
    assert out["host"]["temps_c"] == {"k10temp": 67.5}


# ---------------------------------------------------------------- HTTP layer
@pytest.fixture
def client(env, tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    ex = env[0]
    with TestClient(main_mod.create_app(executor=ex, token=TOKEN)) as c:
        yield c


def test_http_requires_token(client, tmp_path):
    r = client.post("/v1/action", json={"verb": "status"})
    assert r.status_code == 401
    r = client.post("/v1/action", json={"verb": "status"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert "unauthorized" in (tmp_path / "audit.log").read_text()


def test_http_roundtrip(client, tmp_path):
    h = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "discord-123"}
    r = client.post("/v1/action", json={"verb": "restart_stack", "args": {"name": "jellyfin"}}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/v1/action", json={"verb": "restart_stack", "args": {"name": "nope"}}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "rejected"
    log = (tmp_path / "audit.log").read_text()
    assert "discord-123" in log and '"outcome": "rejected"' in log


def test_refuses_short_token():
    with pytest.raises(SystemExit):
        main_mod.create_app(executor=object(), token="short")


# ---------------------------------------------------------------- reload
def test_reload_applies_new_config(env):
    ex, docker, *_ = env
    new = dict(CFG, stacks={"jellyfin": CFG["stacks"]["jellyfin"]})
    ex._config_loader = lambda: ExecutorConfig.model_validate(new)
    out = ex.run("reload_config", {})
    assert out["stacks"] == ["jellyfin"]
    with pytest.raises(Rejected):
        ex.run("restart_stack", {"name": "torrent"})


def test_reload_keeps_old_config_on_error(env):
    ex, *_ = env
    def bad():
        return ExecutorConfig.model_validate({"stacks": {"x": {"restart_sequence": []}}})
    ex._config_loader = bad
    with pytest.raises(ActionFailed):
        ex.run("reload_config", {})
    assert "torrent" in ex.cfg.stacks


def test_reload_rejects_args(env):
    ex, *_ = env
    ex._config_loader = lambda: ex.cfg
    with pytest.raises(Rejected):
        ex.run("reload_config", {"path": "/etc/shadow"})


def test_retry_recovers_stuck_vpn(env):
    """A VPN client stuck on a server that rejects logins (AUTH_FAILED);
    a second restart picks another server and comes up healthy."""
    ex, docker, *_ = env
    cfg = dict(CFG)
    cfg["stacks"] = {"torrent": {"restart_sequence": [
        {"container": "gluetun", "wait_healthy": True, "timeout_seconds": 10, "retries": 1},
        {"container": "transmission"}]}}
    ex.cfg = ExecutorConfig.model_validate(cfg)
    gluetun = docker.get("gluetun")
    gluetun._health = ["starting"] * 6 + ["healthy"]   # first attempt times out, second succeeds
    out = ex.run("restart_stack", {"name": "torrent"})
    assert gluetun.restarts == 2
    assert out["steps"][0] == {"container": "gluetun", "result": "healthy", "attempts": 2}
    assert docker.get("transmission").restarts == 1


def test_retries_exhausted_still_skips_later_steps(env):
    ex, docker, *_ = env
    cfg = dict(CFG)
    cfg["stacks"] = {"torrent": {"restart_sequence": [
        {"container": "gluetun", "wait_healthy": True, "timeout_seconds": 10, "retries": 2},
        {"container": "transmission"}]}}
    ex.cfg = ExecutorConfig.model_validate(cfg)
    docker.get("gluetun")._health = ["unhealthy"] * 100
    with pytest.raises(ActionFailed, match="3 attempt"):
        ex.run("restart_stack", {"name": "torrent"})
    assert docker.get("gluetun").restarts == 3 and docker.get("transmission").restarts == 0
