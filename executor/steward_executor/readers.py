"""Read-only integrations. Everything here only reads: no verb in this file changes
anything on the server. Each takes its dependencies as arguments so tests can fake them."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import datetime
from typing import Any

import httpx

from .config import BackupLogConfig

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s")
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pass|token|secret|api[_-]?key|apikey|authorization|bearer)\b(\s*[:=]\s*|\s+)(\S+)"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),                     # long opaque strings (keys, tokens)
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),                       # user:pass@ in URLs
]
USER_RE = re.compile(r"^[A-Za-z0-9._@+\- ]{1,64}$")


class ReaderError(Exception):
    """A read failed (service down, bad response). Message is safe to show the owner."""


class UnknownUser(Exception):
    pass


# ---------------------------------------------------------------- backups
def _tail_text(path: str, max_bytes: int = 2_000_000) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def _ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()


def backup_status(cfg: BackupLogConfig) -> dict:
    """Most recent run, and the last successful run (overall and per repository)."""
    try:
        text = _tail_text(cfg.path)
    except OSError as exc:
        raise ReaderError(f"can't read the backup log ({exc.strerror or exc})") from None
    started, finished = re.compile(cfg.started_pattern), re.compile(cfg.finished_pattern)
    failed, repo_ok = re.compile(cfg.failed_pattern), re.compile(cfg.repo_ok_pattern)

    last_run: dict | None = None
    last_success: float | None = None
    current_start: float | None = None
    repos: dict[str, float] = {}
    running = False
    for line in text.splitlines():
        ts = _ts(line)
        if ts is None:
            continue
        if started.search(line):
            current_start, running = ts, True
        elif finished.search(line):
            last_run = {"outcome": "ok", "at": ts, "started": current_start}
            last_success, running = ts, False
        elif m := failed.search(line):
            last_run = {"outcome": "failed", "at": ts, "started": current_start,
                        "message": (m.groupdict().get("message") or line)[:300]}
            running = False
        elif m := repo_ok.search(line):
            repos[m.group("repo")] = ts
    return {
        "last_run": last_run,
        "last_success": last_success,
        "repos_last_ok": repos,
        "running_since": current_start if running else None,
    }


# ---------------------------------------------------------------- VPN (Gluetun log)
VPN_IP_RE = re.compile(r"Public IP address is (\S+)(?: \(([^)]*?)(?: - source:[^)]*)?\))?")
VPN_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


def vpn_status(container: Any) -> dict:
    state = container.attrs.get("State", {})
    out = {"state": state.get("Status", "unknown"), "health": state.get("Health", {}).get("Status")}
    logs = container.logs(tail=3000).decode("utf-8", errors="replace").splitlines()
    for line in reversed(logs):
        m = VPN_IP_RE.search(line)
        if m:
            out["public_ip"] = m.group(1)
            if m.group(2):
                out["location"] = m.group(2).strip()
            t = VPN_TS_RE.match(line)
            if t:
                try:
                    out["since"] = datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            break
    return out


# ---------------------------------------------------------------- container logs
def redact(line: str) -> str:
    line = SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", line)
    line = SECRET_PATTERNS[2].sub("://[redacted]@", line)
    return SECRET_PATTERNS[1].sub("[redacted]", line)


def container_logs(container: Any, lines: int) -> dict:
    raw = container.logs(tail=500).decode("utf-8", errors="replace").splitlines()
    problems = sum(1 for l in raw if re.search(r"(?i)\b(error|fatal|panic|exception|fail(ed|ure)?)\b", l))
    tail = [redact(l)[:300] for l in raw[-lines:]]
    return {"container": container.name, "lines": tail, "problem_lines_in_last_500": problems}


# ---------------------------------------------------------------- Immich
def immich_storage(http: httpx.Client, url: str, user: str | None) -> dict:
    resp = http.get(f"{url.rstrip('/')}/api/server/statistics")
    if resp.status_code >= 400:
        raise ReaderError(f"Immich returned HTTP {resp.status_code}")
    data = resp.json()
    users = [{
        "user": u.get("userName") or u.get("userId", "?"),
        "bytes": int(u.get("usage") or 0),
        "photos": int(u.get("photos") or 0),
        "videos": int(u.get("videos") or 0),
        "quota_bytes": u.get("quotaSizeInBytes"),
    } for u in data.get("usageByUser", [])]
    return _filter_users("immich", users, user, total=int(data.get("usage") or sum(u["bytes"] for u in users)))


def immich_jobs(http: httpx.Client, url: str) -> dict:
    resp = http.get(f"{url.rstrip('/')}/api/jobs")
    if resp.status_code >= 400:
        raise ReaderError(f"Immich returned HTTP {resp.status_code}")
    out = {}
    for name, q in resp.json().items():
        counts = q.get("jobCounts", {}) or {}
        status = q.get("queueStatus", {}) or {}
        out[name] = {"active": counts.get("active", 0), "waiting": counts.get("waiting", 0),
                     "failed": counts.get("failed", 0), "paused": bool(status.get("isPaused"))}
    return {"queues": out}


# ---------------------------------------------------------------- Nextcloud (occ)
def _occ(container: Any, *args: str) -> Any:
    res = container.exec_run(["php", "occ", *args, "--output=json"], user="www-data", demux=False)
    if res.exit_code != 0:
        raise ReaderError(f"occ {args[0]} failed (exit {res.exit_code})")
    try:
        return json.loads(res.output.decode("utf-8", errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ReaderError(f"occ {args[0]} returned something unexpected") from None


def nextcloud_storage(container: Any, user: str | None, max_users: int = 200) -> dict:
    ids = list(_occ(container, "user:list"))[:max_users]
    wanted = ids
    if user is not None:
        match = [i for i in ids if i.lower() == user.lower()]
        if not match:
            raise UnknownUser(user)
        wanted = match                         # only real IDs from occ ever reach the command line
    users = []
    for uid in wanted:
        info = _occ(container, "user:info", uid)
        storage = info.get("storage", {}) or {}
        quota = storage.get("quota")
        users.append({"user": info.get("display_name") or uid, "bytes": int(storage.get("used") or 0),
                      "quota_bytes": quota if isinstance(quota, int) and quota > 0 else None})
    return _filter_users("nextcloud", users, None, total=sum(u["bytes"] for u in users),
                         all_users=len(ids))


def _filter_users(app: str, users: list[dict], user: str | None, total: int, all_users: int | None = None) -> dict:
    if user is not None:
        users = [u for u in users if u["user"].lower() == user.lower()]
        if not users:
            raise UnknownUser(user)
    users.sort(key=lambda u: u["bytes"], reverse=True)
    return {"app": app, "users": users, "total_bytes": total,
            "user_count": all_users if all_users is not None else len(users)}


# ---------------------------------------------------------------- Transmission
TR_STATUS = {0: "stopped", 1: "check queued", 2: "checking", 3: "queued", 4: "downloading",
             5: "seed queued", 6: "seeding"}


class TransmissionRPC:
    def __init__(self, http: httpx.Client, url: str, user: str = "", password: str = ""):
        self.http, self.url = http, url
        self.auth = (user, password) if user else None
        self.session_id = ""

    def call(self, method: str, arguments: dict | None = None) -> dict:
        body = {"method": method, "arguments": arguments or {}}
        for _ in range(2):
            resp = self.http.post(self.url, json=body, auth=self.auth,
                                  headers={"X-Transmission-Session-Id": self.session_id})
            if resp.status_code == 409:      # CSRF handshake: retry with the new session id
                self.session_id = resp.headers.get("X-Transmission-Session-Id", "")
                continue
            if resp.status_code == 401:
                raise ReaderError("Transmission rejected the login (set TRANSMISSION_USER/TRANSMISSION_PASS)")
            if resp.status_code >= 400:
                raise ReaderError(f"Transmission returned HTTP {resp.status_code}")
            data = resp.json()
            if data.get("result") != "success":
                raise ReaderError(f"Transmission: {data.get('result')}")
            return data.get("arguments", {})
        raise ReaderError("Transmission session handshake failed")


def torrent_list(rpc: TransmissionRPC) -> dict:
    fields = ["name", "percentDone", "rateDownload", "eta", "status", "totalSize", "error", "errorString",
              "queuePosition"]
    torrents = rpc.call("torrent-get", {"fields": fields}).get("torrents", [])
    out = [{
        "name": t.get("name", "?")[:120],
        "percent": round(100 * float(t.get("percentDone") or 0), 1),
        "rate_bps": int(t.get("rateDownload") or 0),
        "eta_s": t.get("eta") if (t.get("eta") or -1) >= 0 else None,
        "status": TR_STATUS.get(t.get("status"), "unknown"),
        "size_bytes": int(t.get("totalSize") or 0),
        "error": (t.get("errorString") or "")[:120] if t.get("error") else "",
        "queue": t.get("queuePosition"),
    } for t in torrents]
    order = {"downloading": 0, "checking": 1, "queued": 2, "check queued": 2, "stopped": 3}
    out.sort(key=lambda t: (order.get(t["status"], 4), t["queue"] if t["queue"] is not None else 9999))
    return {"torrents": out, "count": len(out)}


# ---------------------------------------------------------------- Jellyfin
def _where(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return "unknown"
    if addr in ipaddress.ip_network("100.64.0.0/10") or addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return "tailnet"
    if addr.is_private or addr.is_loopback:
        return "home network"
    return "internet"


def jellyfin_streams(http: httpx.Client, url: str, api_key: str) -> dict:
    if not api_key:
        raise ReaderError("JELLYFIN_API_KEY is not set")
    resp = http.get(f"{url.rstrip('/')}/Sessions", params={"activeWithinSeconds": 960},
                    headers={"Authorization": f'MediaBrowser Token="{api_key}"'})
    if resp.status_code >= 400:
        raise ReaderError(f"Jellyfin returned HTTP {resp.status_code}")
    streams = []
    for s in resp.json():
        item = s.get("NowPlayingItem")
        if not item:
            continue
        title = item.get("Name", "?")
        if item.get("SeriesName"):
            se = ""
            if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
                se = f" S{item['ParentIndexNumber']:02d}E{item['IndexNumber']:02d}"
            title = f"{item['SeriesName']}{se}: {title}"
        streams.append({
            "app": "jellyfin",
            "user": s.get("UserName", "?"),
            "title": title[:120],
            "client": s.get("Client") or s.get("DeviceName") or "?",
            "where": _where(s.get("RemoteEndPoint", "")),
            "paused": bool((s.get("PlayState") or {}).get("IsPaused")),
            "transcoding": (s.get("PlayState") or {}).get("PlayMethod") == "Transcode",
        })
    return {"streams": streams}


# ---------------------------------------------------------------- Audiobookshelf
def audiobookshelf_streams(http: httpx.Client, url: str, token: str) -> dict:
    if not token:
        raise ReaderError("AUDIOBOOKSHELF_TOKEN is not set")
    resp = http.get(f"{url.rstrip('/')}/api/users/online", headers={"Authorization": f"Bearer {token}"})
    if resp.status_code in (401, 403):
        raise ReaderError("Audiobookshelf rejected the token (it needs an admin user's token)")
    if resp.status_code >= 400:
        raise ReaderError(f"Audiobookshelf returned HTTP {resp.status_code}")
    data = resp.json()
    names = {u.get("id"): u.get("username") for u in data.get("usersOnline", []) if u.get("id")}
    sessions = list(data.get("openSessions") or [])
    if not sessions:   # some versions only nest the session under each online user
        sessions = [dict(u["session"], userId=u.get("id")) for u in data.get("usersOnline", []) if u.get("session")]
    # Third-party client apps may not show up in usersOnline; resolve IDs from the user list.
    if any(ses.get("userId") and ses.get("userId") not in names for ses in sessions):
        try:
            ur = http.get(f"{url.rstrip('/')}/api/users", headers={"Authorization": f"Bearer {token}"})
            if ur.status_code < 400:
                for u in ur.json().get("users", []):
                    names.setdefault(u.get("id"), u.get("username"))
        except Exception:
            pass   # names are a nicety; keep the sessions
    streams = []
    for ses in sessions:
        dev = ses.get("deviceInfo") or {}
        title = ses.get("displayTitle") or "?"
        if ses.get("displayAuthor"):
            title = f"{title} by {ses['displayAuthor']}"
        dur = ses.get("duration") or 0
        cur = max(ses.get("currentTime") or 0, ses.get("startTime") or 0)   # currentTime is 0 until first sync
        streams.append({
            "app": "audiobookshelf",
            "user": names.get(ses.get("userId")) or (ses.get("user") or {}).get("username") or "?",
            "title": title[:120],
            "client": dev.get("clientName") or dev.get("deviceName") or ses.get("mediaPlayer") or "?",
            "where": _where(dev.get("ipAddress", "")),
            "progress_pct": round(100 * cur / dur) if dur else None,
        })
    return {"streams": streams}
