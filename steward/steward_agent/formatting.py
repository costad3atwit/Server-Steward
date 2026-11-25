"""Turn executor results into short Discord messages. Deterministic on purpose: results are
never passed back through the model, so it can't misreport what happened."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .tools import QUEUE_LABELS

STATE_ICON = {"running": "🟢", "restarting": "🟡", "exited": "🔴", "missing": "⚫", "paused": "🟡"}


def fmt_time(ts: float, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%a %b %-d, %-I:%M %p %Z")


def format_status(r: dict) -> str:
    lines = ["**Containers**"]
    for name, c in r.get("containers", {}).items():
        state = c.get("state", "unknown")
        health = c.get("health")
        icon = STATE_ICON.get(state, "⚪")
        if health == "unhealthy":
            icon = "🔴"
        elif health == "starting":
            icon = "🟡"
        extra = f" ({health})" if health and health != "healthy" else ""
        lines.append(f"{icon} {name}: {state}{extra}")

    h = r.get("host", {})
    lines.append("**Host**")
    bits = []
    if "uptime_hours" in h:
        up = h["uptime_hours"]
        bits.append(f"up {up / 24:.1f} days" if up >= 48 else f"up {up:.0f} h")
    if "load" in h:
        bits.append(f"load {h['load'][0]:.2f}")
    if "memory_gb" in h:
        m = h["memory_gb"]
        bits.append(f"RAM {m['total'] - m['available']:.1f}/{m['total']:.1f} GB")
    if bits:
        lines.append(", ".join(bits))
    for d in h.get("disks", []):
        if "error" in d:
            lines.append(f"💾 {d['name']}: {d['error']}")
        else:
            warn = " ⚠️" if d["used_pct"] >= 90 else ""
            lines.append(f"💾 {d['name']}: {d['used_pct']:.0f}% used, {d['free_gb']:.0f} GB free{warn}")
    temps = h.get("temps_c", {})
    if temps:
        hot = max(temps.values())
        warn = " 🔥" if hot >= 85 else ""
        lines.append("🌡️ " + ", ".join(f"{k} {v:.0f}°C" for k, v in temps.items()) + warn)
    return "\n".join(lines)


def fmt_bytes(n: float) -> str:
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def fmt_ago(ts: float, now: float | None = None) -> str:
    import time
    secs = max(0, (now if now is not None else time.time()) - ts)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} days ago"


def fmt_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f} s"
    if secs < 3600:
        return f"{secs / 60:.0f} min"
    if secs < 86400:
        return f"{secs / 3600:.1f} h"
    return f"{secs / 86400:.1f} days"


def format_backups(r: dict, tz: ZoneInfo, now: float | None = None) -> str:
    lines = []
    if r.get("running_since"):
        lines.append(f"⏳ A backup is running now (started {fmt_time(r['running_since'], tz)}).")
    last, good = r.get("last_run"), r.get("last_success")
    if last is None:
        lines.append("No finished backup runs found in the log.")
    elif last["outcome"] == "ok":
        lines.append(f"✅ Last backup finished {fmt_time(last['at'], tz)} ({fmt_ago(last['at'], now)}).")
    else:
        lines.append(f"❌ Last backup FAILED {fmt_time(last['at'], tz)} ({fmt_ago(last['at'], now)}):\n"
                     f"> {last.get('message', 'no details')}")
        if good:
            lines.append(f"Last successful backup: {fmt_time(good, tz)} (**{fmt_ago(good, now)}**).")
        else:
            lines.append("⚠️ No successful backup found in the log.")
    repos = r.get("repos_last_ok") or {}
    if repos:
        lines.append("Each copy last saved: " + ", ".join(
            f"{name} {fmt_ago(ts, now)}" for name, ts in sorted(repos.items())))
    return "\n".join(lines)


def format_vpn(r: dict, tz: ZoneInfo) -> str:
    health = r.get("health") or r.get("state")
    icon = "🛡️" if health == "healthy" else "⚠️"
    msg = f"{icon} VPN container is {health}."
    if r.get("public_ip"):
        where = f" ({r['location']})" if r.get("location") else ""
        when = f", as of {fmt_time(r['since'], tz)}" if r.get("since") else ""
        msg += f" Public IP {r['public_ip']}{where}{when}."
    else:
        msg += " No public IP in its recent log."
    return msg


def format_storage(r: dict) -> str:
    users = r.get("users", [])
    if not users:
        return "No users found."
    is_immich = r.get("app") == "immich"
    width = max(len(u["user"]) for u in users)
    rows = []
    for u in users:
        row = f"{u['user']:<{width}}  {fmt_bytes(u['bytes']):>9}"
        if is_immich:
            row += f"  {u.get('photos', 0):>7,} photos  {u.get('videos', 0):>6,} videos"
        if u.get("quota_bytes"):
            row += f"  (quota {fmt_bytes(u['quota_bytes'])}, {100 * u['bytes'] / u['quota_bytes']:.0f}%)"
        rows.append(row)
    title = f"**{'Immich' if is_immich else 'Nextcloud'} storage**"
    footer = ""
    if len(users) > 1 or r.get("user_count", 1) > 1:
        footer = f"\nTotal: {fmt_bytes(r.get('total_bytes', 0))} across {r.get('user_count', len(users))} users"
    return f"{title}\n```\n" + "\n".join(rows) + f"\n```{footer}"


def format_torrents(r: dict) -> str:
    ts = r.get("torrents", [])
    if not ts:
        return "No torrents."
    lines = [f"**Torrents** ({len(ts)})"]
    for t in ts[:15]:
        bits = [f"{t['percent']:.0f}%", t["status"]]
        if t.get("rate_bps"):
            bits.append(f"↓ {fmt_bytes(t['rate_bps'])}/s")
        if t.get("eta_s"):
            bits.append(f"{fmt_duration(t['eta_s'])} left")
        line = f"• {t['name']}: " + ", ".join(bits)
        if t.get("error"):
            line += f" ⚠️ {t['error']}"
        lines.append(line)
    if len(ts) > 15:
        lines.append(f"…and {len(ts) - 15} more")
    return "\n".join(lines)


APP_ICON = {"jellyfin": "🎬", "audiobookshelf": "🎧"}
APP_NAME = {"jellyfin": "Jellyfin", "audiobookshelf": "Audiobookshelf"}


def format_streams(r: dict) -> str:
    ss = r.get("streams", [])
    lines = []
    if not ss:
        apps = " or ".join(r.get("apps") or ["any app"])
        lines.append(f"Nobody is watching or listening on {apps} right now.")
    else:
        lines.append(f"**Streaming now** ({len(ss)})")
        for s in ss:
            extras = [s["where"]]
            if s.get("progress_pct") is not None:
                extras.append(f"{s['progress_pct']}% through")
            if s.get("paused"):
                extras.append("paused")
            if s.get("transcoding"):
                extras.append("transcoding")
            icon = APP_ICON.get(s.get("app"), "▶️")
            lines.append(f"{icon} {s['user']}: {s['title']} on {s['client']} ({', '.join(extras)})")
    for err in r.get("errors", []):
        lines.append(f"⚠️ Couldn't check {err}")
    return "\n".join(lines)


def format_immich_jobs(r: dict) -> str:
    rows = []
    for name, q in sorted(r.get("queues", {}).items()):
        if not (q["active"] or q["waiting"] or q["failed"] or q["paused"]):
            continue
        bits = []
        if q["paused"]:
            bits.append("⏸️ paused")
        if q["active"]:
            bits.append(f"{q['active']} running")
        if q["waiting"]:
            bits.append(f"{q['waiting']:,} waiting")
        if q["failed"]:
            bits.append(f"{q['failed']:,} failed")
        rows.append(f"• {QUEUE_LABELS.get(name, name)}: {', '.join(bits)}")
    return "**Immich jobs**\n" + "\n".join(rows) if rows else "Immich has no queued work. All quiet."


FENCE = "`" * 3


def format_logs(r: dict) -> str:
    head = f"**{r['container']}**, last {len(r['lines'])} lines"
    if r.get("problem_lines_in_last_500"):
        head += f" ({r['problem_lines_in_last_500']} error-looking lines in the last 500)"
    body, budget = [], 1700 - len(head)
    for line in reversed(r["lines"]):          # keep the newest lines if it's too long
        if budget - len(line) - 1 < 0:
            break
        body.insert(0, line.replace(FENCE, "'''"))
        budget -= len(line) + 1
    return f"{head}\n{FENCE}\n" + "\n".join(body) + f"\n{FENCE}"


def format_result(verb: str, result: dict, tz: ZoneInfo) -> str:
    if verb == "status":
        return format_status(result)
    if verb == "backup_status":
        return format_backups(result, tz)
    if verb == "vpn_status":
        return format_vpn(result, tz)
    if verb == "storage_usage":
        return format_storage(result)
    if verb == "torrent_list":
        return format_torrents(result)
    if verb == "streams":
        return format_streams(result)
    if verb == "immich_jobs":
        return format_immich_jobs(result)
    if verb == "container_logs":
        return format_logs(result)
    if verb == "restart_stack":
        steps = ", ".join(
            f"{s['container']} ({s['result']}" + (f", {s['attempts']} tries" if s.get("attempts", 1) > 1 else "") + ")"
            for s in result.get("steps", []))
        return f"✅ Restarted **{result.get('stack')}**: {steps}"
    if verb == "immich_queue":
        q = QUEUE_LABELS.get(result.get("queue"), result.get("queue"))
        if result.get("command") == "pause":
            msg = f"⏸️ Paused Immich **{q}**."
            if "resume_at" in result:
                msg += f" It resumes automatically {fmt_time(result['resume_at'], tz)}."
            return msg
        msg = f"▶️ Resumed Immich **{q}**."
        if result.get("cancelled_schedule"):
            msg += " Cancelled the scheduled resume."
        return msg
    if verb == "list_schedules":
        items = result.get("schedules", [])
        if not items:
            return "Nothing scheduled."
        lines = ["**Scheduled**"]
        for s in items:
            a = s.get("args", {})
            what = f"{a.get('action', s['verb'])} {QUEUE_LABELS.get(a.get('queue'), a.get('queue', ''))}".strip()
            lines.append(f"• {what}: {fmt_time(s['run_at'], tz)}")
        return "\n".join(lines)
    if verb == "reload_config":
        stacks = ", ".join(result.get("stacks", [])) or "none"
        immich = "on" if result.get("immich_enabled") else "off"
        return f"🔄 Config reloaded. Stacks: {stacks}. Immich: {immich}."
    return f"Done: `{result}`"


def format_failure(status: int, body: dict) -> str:
    detail = body.get("detail", "unknown error")
    kind = body.get("error")
    if kind == "rejected":
        return f"🚫 The executor refused: {detail}"
    if kind == "unreachable":
        return f"⚠️ Couldn't reach the executor: {detail}"
    msg = f"❌ Failed: {detail}"
    partial = body.get("partial")
    if isinstance(partial, dict) and partial.get("steps"):
        steps = ", ".join(f"{s['container']} ({s.get('result')})" for s in partial["steps"])
        msg += f"\nSteps so far: {steps}"
    return msg
