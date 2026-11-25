import json
from datetime import datetime

import httpx
import pytest

from steward_executor import readers
from steward_executor.actions import ActionFailed, Executor, Rejected
from steward_executor.config import BackupLogConfig, ExecutorConfig
from steward_executor.schedules import ScheduleStore

BACKUP_LOG = """\
2026-11-14 03:00:21 ===== backup finished
2026-11-17 04:01:00 FAILED: /mnt/backup is not mounted (dock off or drive missing); nothing was backed up
2026-11-23 03:00:01 FAILED: /mnt/backup is not mounted (dock off or drive missing); nothing was backed up
2026-11-23 14:38:18 ===== backup started
2026-11-23 14:38:21 dumped immich (372M)
2026-11-23 16:10:02 vault: backup OK
2026-11-23 18:55:40 nvme: backup OK
2026-11-23 18:59:00 ===== backup finished
2026-11-24 03:00:00 ===== backup started
2026-11-24 03:20:00 vault: backup OK
2026-11-24 03:30:00 FAILED: nvme: only 40 GB free on the root disk (minimum 60), so the NVMe copy was skipped (see /var/log/backup.log)
"""


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


# ---------------------------------------------------------------- backups
def test_backup_failed_run_reports_last_success(tmp_path):
    log = tmp_path / "b.log"
    log.write_text(BACKUP_LOG)
    r = readers.backup_status(BackupLogConfig(enabled=True, path=str(log)))
    assert r["last_run"]["outcome"] == "failed" and "only 40 GB" in r["last_run"]["message"]
    assert r["last_run"]["at"] == ts("2026-11-24 03:30:00")
    assert r["last_success"] == ts("2026-11-23 18:59:00")
    assert r["repos_last_ok"] == {"vault": ts("2026-11-24 03:20:00"), "nvme": ts("2026-11-23 18:55:40")}
    assert r["running_since"] is None


def test_backup_running_now(tmp_path):
    log = tmp_path / "b.log"
    log.write_text(BACKUP_LOG.split("2026-11-23 16:10")[0])
    r = readers.backup_status(BackupLogConfig(enabled=True, path=str(log)))
    assert r["running_since"] == ts("2026-11-23 14:38:18")
    assert r["last_success"] == ts("2026-11-14 03:00:21")


def test_backup_log_missing(tmp_path):
    with pytest.raises(readers.ReaderError):
        readers.backup_status(BackupLogConfig(enabled=True, path=str(tmp_path / "nope.log")))


# ---------------------------------------------------------------- fakes
class FakeContainer:
    def __init__(self, name, logs="", exec_map=None, health="healthy"):
        self.name = name
        self._logs = logs
        self.exec_map = exec_map or {}
        self.exec_calls = []
        self.attrs = {"State": {"Status": "running", "Health": {"Status": health}}}

    def logs(self, tail=100):
        return "\n".join(self._logs.splitlines()[-tail:]).encode()

    def exec_run(self, cmd, user=None, demux=False):
        self.exec_calls.append((cmd, user))
        key = " ".join(cmd[2:-1])
        out = self.exec_map.get(key)

        class R:
            exit_code = 0 if out is not None else 1
            output = (json.dumps(out) if out is not None else "").encode()
        return R()


class FakeDocker:
    def __init__(self, *containers):
        self._c = {c.name: c for c in containers}
        self.containers = self

    def get(self, name):
        return self._c[name]


def mock_http(routes):
    def handler(request: httpx.Request):
        for (method, path), fn in routes.items():
            if request.method == method and request.url.path == path:
                return fn(request)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


CFG = {
    "stacks": {"torrent": {"restart_sequence": [{"container": "gluetun"}, {"container": "transmission"}]}},
    "integrations": {
        "immich": {"enabled": True, "url": "http://immich:2283", "queues": ["videoConversion"]},
        "nextcloud": {"enabled": True},
        "transmission": {"enabled": True, "url": "http://gluetun:9091/transmission/rpc"},
        "jellyfin": {"enabled": True, "url": "http://jellyfin:8096"},
        "vpn": {"enabled": True},
    },
}

IMMICH_STATS = {"photos": 50000, "videos": 3000, "usage": 300_000_000_000, "usageByUser": [
    {"userId": "u1", "userName": "alice", "photos": 45000, "videos": 2800, "usage": 280_000_000_000,
     "quotaSizeInBytes": None},
    {"userId": "u2", "userName": "bob", "photos": 5000, "videos": 200, "usage": 20_000_000_000,
     "quotaSizeInBytes": 100_000_000_000},
]}

NC = {
    "user:list": {"alice": "alice", "bob": "Bob"},
    "user:info alice": {"user_id": "alice", "display_name": "alice",
                          "storage": {"used": 66060005589, "quota": -3}},
    "user:info bob": {"user_id": "bob", "display_name": "Bob", "storage": {"used": 1_000_000, "quota": 5_000_000_000}},
}

GLUETUN_LOG = """\
2026-11-23T14:04:44-05:00 INFO [openvpn] Initialization Sequence Completed
2026-11-23T14:04:47-05:00 INFO [ip getter] Public IP address is 203.0.113.7 (Netherlands, North Holland, Amsterdam - source: ipinfo+ifconfig.co+ip2location+cloudflare)
2026-11-23T14:04:48-05:00 ERROR [vpn] cannot get version information
"""


def make(tmp_path, http=None, secrets=None, **containers):
    nc = FakeContainer("nextcloud-app-1", exec_map=NC)
    gl = FakeContainer("gluetun", logs=GLUETUN_LOG)
    tr = FakeContainer("transmission", logs="ok\nERROR token=abc123SECRET\nconnected to http://bob:hunter2@host/rpc\n"
                       + "key " + "A" * 40)
    return Executor(ExecutorConfig.model_validate(CFG), FakeDocker(nc, gl, tr),
                    ScheduleStore(tmp_path / "s.json"), http=http,
                    secrets=secrets if secrets is not None else {"IMMICH_API_KEY": "k", "JELLYFIN_API_KEY": "j"})


# ---------------------------------------------------------------- storage
def test_immich_storage_all_and_one_user(tmp_path):
    seen = {}

    def stats(req):
        seen["key"] = req.headers.get("x-api-key")
        return httpx.Response(200, json=IMMICH_STATS)

    ex = make(tmp_path, http=mock_http({("GET", "/api/server/statistics"): stats}))
    r = ex.run("storage_usage", {"app": "immich"})
    assert [u["user"] for u in r["users"]] == ["alice", "bob"] and r["total_bytes"] == 300_000_000_000
    assert seen["key"] == "k"
    r = ex.run("storage_usage", {"app": "immich", "user": "BOB"})
    assert len(r["users"]) == 1 and r["users"][0]["photos"] == 5000
    with pytest.raises(Rejected, match="no user"):
        ex.run("storage_usage", {"app": "immich", "user": "Nobody"})


def test_nextcloud_storage_only_real_ids_reach_occ(tmp_path):
    ex = make(tmp_path)
    r = ex.run("storage_usage", {"app": "nextcloud"})
    assert [u["user"] for u in r["users"]] == ["alice", "Bob"]
    assert r["users"][0]["quota_bytes"] is None and r["users"][1]["quota_bytes"] == 5_000_000_000
    nc = ex.docker.get("nextcloud-app-1")
    nc.exec_calls.clear()
    ex.run("storage_usage", {"app": "nextcloud", "user": "BOB"})
    assert nc.exec_calls[-1] == (["php", "occ", "user:info", "bob", "--output=json"], "www-data")
    nc.exec_calls.clear()
    with pytest.raises(Rejected):
        ex.run("storage_usage", {"app": "nextcloud", "user": "x; rm -rf /"})
    assert nc.exec_calls == []
    with pytest.raises(Rejected, match="no user"):
        ex.run("storage_usage", {"app": "nextcloud", "user": "eve"})
    assert all(c[0][2] == "user:list" for c in nc.exec_calls)


def test_storage_rejects_unknown_app(tmp_path):
    with pytest.raises(Rejected):
        make(tmp_path).run("storage_usage", {"app": "jellyfin"})


# ---------------------------------------------------------------- vpn, logs
def test_vpn_status_from_gluetun_log(tmp_path):
    r = make(tmp_path).run("vpn_status", {})
    assert r["public_ip"] == "203.0.113.7" and r["location"] == "Netherlands, North Holland, Amsterdam"
    assert r["health"] == "healthy" and r["since"] > 0


def test_container_logs_redacts_and_allowlists(tmp_path):
    ex = make(tmp_path)
    r = ex.run("container_logs", {"container": "transmission", "lines": 5})
    text = "\n".join(r["lines"])
    assert "abc123SECRET" not in text and "hunter2" not in text and "A" * 40 not in text
    assert "[redacted]" in text and r["problem_lines_in_last_500"] == 1
    with pytest.raises(Rejected):
        ex.run("container_logs", {"container": "nextcloud-app-1"})      # not in stacks/extra
    with pytest.raises(Rejected):
        ex.run("container_logs", {"container": "transmission", "lines": 5000})


# ---------------------------------------------------------------- transmission
def test_torrent_list_handles_session_handshake_and_auth(tmp_path):
    state = {"calls": 0}

    def rpc(req):
        state["calls"] += 1
        if req.headers.get("X-Transmission-Session-Id") != "sid":
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "sid"})
        assert req.headers.get("Authorization", "").startswith("Basic ")
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": [
            {"name": "Done.iso", "percentDone": 1, "status": 0, "eta": -1, "queuePosition": 1},
            {"name": "Ubuntu.iso", "percentDone": 0.453, "rateDownload": 2_300_000, "eta": 720,
             "status": 4, "totalSize": 5_000_000_000, "queuePosition": 0},
        ]}})

    ex = make(tmp_path, http=mock_http({("POST", "/transmission/rpc"): rpc}),
              secrets={"TRANSMISSION_USER": "u", "TRANSMISSION_PASS": "p"})
    r = ex.run("torrent_list", {})
    assert [t["name"] for t in r["torrents"]] == ["Ubuntu.iso", "Done.iso"]
    assert r["torrents"][0]["percent"] == 45.3 and r["torrents"][0]["eta_s"] == 720
    assert r["torrents"][1]["eta_s"] is None and state["calls"] == 2


def test_torrent_list_bad_login_is_reported(tmp_path):
    ex = make(tmp_path, http=mock_http({("POST", "/transmission/rpc"): lambda r: httpx.Response(401)}))
    with pytest.raises(ActionFailed, match="login"):
        ex.run("torrent_list", {})


# ---------------------------------------------------------------- jellyfin, immich jobs
def test_streams(tmp_path):
    sessions = [
        {"UserName": "bob", "Client": "Roku", "RemoteEndPoint": "9.9.9.9",
         "NowPlayingItem": {"Name": "Dinner Party", "SeriesName": "The Office", "ParentIndexNumber": 4,
                            "IndexNumber": 13}, "PlayState": {"IsPaused": True, "PlayMethod": "Transcode"}},
        {"UserName": "alice", "Client": "Swiftfin", "RemoteEndPoint": "100.101.2.3",
         "NowPlayingItem": {"Name": "Heat"}, "PlayState": {}},
        {"UserName": "Idle", "Client": "Web", "RemoteEndPoint": "10.0.0.5"},
    ]
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=sessions)

    r = make(tmp_path, http=mock_http({("GET", "/Sessions"): handler})).run("streams", {})
    assert seen["auth"] == 'MediaBrowser Token="j"'
    assert r["streams"][0] == {"app": "jellyfin", "user": "bob", "title": "The Office S04E13: Dinner Party", "client": "Roku",
                               "where": "internet", "paused": True, "transcoding": True}
    assert r["streams"][1]["where"] == "tailnet" and len(r["streams"]) == 2


def test_immich_jobs(tmp_path):
    jobs = {"videoConversion": {"jobCounts": {"active": 1, "waiting": 40, "failed": 0},
                                "queueStatus": {"isPaused": False}},
            "smartSearch": {"jobCounts": {"active": 0, "waiting": 0}, "queueStatus": {"isPaused": True}}}
    r = make(tmp_path, http=mock_http({("GET", "/api/jobs"): lambda q: httpx.Response(200, json=jobs)})
             ).run("immich_jobs", {})
    assert r["queues"]["videoConversion"]["waiting"] == 40 and r["queues"]["smartSearch"]["paused"]


def test_disabled_integration_rejected(tmp_path):
    ex = make(tmp_path)
    with pytest.raises(Rejected, match="backup log"):
        ex.run("backup_status", {})


def test_service_down_is_a_clean_failure(tmp_path):
    def boom(req):
        raise httpx.ConnectError("refused")
    ex = make(tmp_path, http=mock_http({("GET", "/api/jobs"): boom}))
    with pytest.raises(ActionFailed, match="ConnectError"):
        ex.run("immich_jobs", {})


# ---------------------------------------------------------------- streams across apps
ABS_ONLINE = {
    "usersOnline": [{"id": "u-bob", "username": "bob"}, {"id": "u-carol", "username": "carol"}],
    "openSessions": [{"userId": "u-bob", "displayTitle": "Project Hail Mary", "displayAuthor": "Andy Weir",
                      "currentTime": 3600, "duration": 57600, "mediaPlayer": "html5",
                      "deviceInfo": {"clientName": "Abs Android", "ipAddress": "100.70.1.2"}}],
}


def abs_cfg(tmp_path, http, secrets, jellyfin=True):
    cfg = dict(CFG)
    cfg["integrations"] = {**CFG["integrations"], "jellyfin": {"enabled": jellyfin, "url": "http://jellyfin:8096"},
                           "audiobookshelf": {"enabled": True, "url": "http://audiobookshelf:80"}}
    return Executor(ExecutorConfig.model_validate(cfg), FakeDocker(), ScheduleStore(tmp_path / "s.json"),
                    http=http, secrets=secrets)


def test_streams_combine_jellyfin_and_audiobookshelf(tmp_path):
    seen = {}

    def online(req):
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json=ABS_ONLINE)

    http = mock_http({("GET", "/Sessions"): lambda r: httpx.Response(200, json=[
        {"UserName": "alice", "Client": "Jellyfin iOS", "RemoteEndPoint": "10.0.0.5",
         "NowPlayingItem": {"Name": "Heat"}, "PlayState": {}}]),
        ("GET", "/api/users/online"): online})
    r = abs_cfg(tmp_path, http, {"JELLYFIN_API_KEY": "j", "AUDIOBOOKSHELF_TOKEN": "t"}).run("streams", {})
    assert seen["auth"] == "Bearer t"
    assert [s["app"] for s in r["streams"]] == ["jellyfin", "audiobookshelf"]
    book = r["streams"][1]
    assert book["user"] == "bob" and book["title"] == "Project Hail Mary by Andy Weir"
    assert book["where"] == "tailnet" and book["progress_pct"] == 6 and r["errors"] == []


def test_one_app_down_still_reports_the_other(tmp_path):
    http = mock_http({("GET", "/Sessions"): lambda r: httpx.Response(500),
                      ("GET", "/api/users/online"): lambda r: httpx.Response(200, json=ABS_ONLINE)})
    r = abs_cfg(tmp_path, http, {"JELLYFIN_API_KEY": "j", "AUDIOBOOKSHELF_TOKEN": "t"}).run("streams", {})
    assert len(r["streams"]) == 1 and r["errors"][0].startswith("Jellyfin:")


def test_abs_bad_token_and_all_down(tmp_path):
    http = mock_http({("GET", "/api/users/online"): lambda r: httpx.Response(401)})
    with pytest.raises(ActionFailed, match="admin user's token"):
        abs_cfg(tmp_path, http, {"AUDIOBOOKSHELF_TOKEN": "t"}, jellyfin=False).run("streams", {})


def test_abs_sessions_nested_under_users(tmp_path):
    nested = {"usersOnline": [{"id": "u1", "username": "Kid", "session": {
        "displayTitle": "Dune", "deviceInfo": {"deviceName": "iPad", "ipAddress": "73.1.2.3"}}}]}
    http = mock_http({("GET", "/api/users/online"): lambda r: httpx.Response(200, json=nested)})
    r = abs_cfg(tmp_path, http, {"AUDIOBOOKSHELF_TOKEN": "t"}, jellyfin=False).run("streams", {})
    assert r["streams"][0]["user"] == "Kid" and r["streams"][0]["where"] == "internet"


def test_abs_user_resolved_from_user_list(tmp_path):
    """A third-party client session whose user isn't in the online list."""
    online = {"usersOnline": [], "openSessions": [
        {"userId": "u-alice", "displayTitle": "Example Book", "displayAuthor": "A. Writer",
         "currentTime": 0, "startTime": 0, "duration": 40000, "deviceInfo": {"clientName": "Third-party app",
                                                                            "ipAddress": "100.70.1.2"}},
        {"userId": "u-bob", "displayTitle": "Dune", "currentTime": 0, "startTime": 20000, "duration": 80000,
         "deviceInfo": {"clientName": "Abs Android", "ipAddress": "10.0.0.7"}}]}
    users = {"users": [{"id": "u-alice", "username": "alice"}, {"id": "u-bob", "username": "bob"}]}
    http = mock_http({("GET", "/api/users/online"): lambda r: httpx.Response(200, json=online),
                      ("GET", "/api/users"): lambda r: httpx.Response(200, json=users)})
    r = abs_cfg(tmp_path, http, {"AUDIOBOOKSHELF_TOKEN": "t"}, jellyfin=False).run("streams", {})
    a, b = r["streams"]
    assert a["user"] == "alice" and a["progress_pct"] == 0
    assert b["user"] == "bob" and b["progress_pct"] == 25
