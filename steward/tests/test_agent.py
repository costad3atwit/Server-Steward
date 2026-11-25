import asyncio
from zoneinfo import ZoneInfo

import pytest

from steward_agent.agent import Agent, Proposal, Reply
from steward_agent.config import AgentConfig
from steward_agent.formatting import format_result, format_status
from steward_agent.tools import ToolError, build_tools, validate_call

CFG = AgentConfig.model_validate({
    "llm": {"chat_model": "m", "options": {"num_ctx": 8192, "num_gpu": 99}},
    "stacks": {"torrent": {"description": "VPN and Transmission"}, "jellyfin": {"description": "Media"}},
    "integrations": {"immich": {"enabled": True, "queues": ["videoConversion", "smartSearch"],
                                "max_pause_hours": 48}},
    "status": {"disks": []},  # executor-only keys are ignored
})
TZ = ZoneInfo("America/New_York")


def run(coro):
    return asyncio.run(coro)


class FakeLLM:
    def __init__(self, message):
        self.message = message
        self.calls = []

    async def chat(self, model, messages, tools, options=None):
        self.calls.append({"model": model, "tools": tools, "options": options})
        return self.message

    async def is_loaded(self, model):
        return True


class FakeExecutor:
    def __init__(self, status=200, body=None):
        self.status, self.body = status, body or {"ok": True, "result": {}}
        self.calls = []

    async def run(self, verb, args, request_id=""):
        self.calls.append((verb, args, request_id))
        return self.status, self.body


def tool_msg(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def make_agent(message, executor=None):
    return Agent(lambda: CFG, FakeLLM(message), executor or FakeExecutor(), TZ)


# ---------------------------------------------------------------- registry
def test_schema_only_lists_configured_names():
    tools = build_tools(CFG)
    assert set(tools) == {"get_status", "restart_stack", "immich_queue", "list_schedules", "reload_config",
                          "storage_usage"}    # immich on -> per-user storage; no stacks containers -> no logs
    assert tools["restart_stack"].parameters["properties"]["name"]["enum"] == ["jellyfin", "torrent"]
    assert tools["immich_queue"].parameters["properties"]["hours"]["maximum"] == 48


def test_immich_tool_absent_when_disabled():
    cfg = CFG.model_copy(update={"integrations": AgentConfig().integrations})
    assert "immich_queue" not in build_tools(cfg)


@pytest.mark.parametrize("name,args,msg", [
    ("reboot_server", {}, "no action"),
    ("restart_stack", {"name": "nextcloud"}, "invalid"),
    ("restart_stack", {}, "invalid"),
    ("restart_stack", {"name": "torrent", "force": True}, "invalid"),
    ("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 168}, "at most 48"),
    ("immich_queue", {"action": "delete", "queue": "videoConversion"}, "invalid"),
    ("get_status", "not a dict", "malformed"),
])
def test_validation_rejects(name, args, msg):
    with pytest.raises(ToolError, match=msg):
        validate_call(build_tools(CFG), name, args)


def test_validation_coerces_small_model_quirks():
    spec, args = validate_call(build_tools(CFG), "immich_queue",
                               {"action": "pause", "queue": "videoConversion", "hours": "24"})
    assert args["hours"] == 24
    _, args = validate_call(build_tools(CFG), "immich_queue",
                            {"action": "resume", "queue": "videoConversion", "hours": None})
    assert "hours" not in args
    _, args = validate_call(build_tools(CFG), "immich_queue",
                            {"action": "resume", "queue": "videoConversion", "hours": 3})
    assert "hours" not in args


# ---------------------------------------------------------------- planning
def test_restart_becomes_confirmed_proposal():
    agent = make_agent(tool_msg("restart_stack", {"name": "torrent"}))
    plan = run(agent.plan("bounce the torrent container", "r1"))
    assert isinstance(plan, Proposal) and plan.needs_confirmation
    assert "torrent" in plan.summary
    assert agent.llm.calls[0]["options"]["num_gpu"] == 99


def test_status_runs_without_confirmation():
    plan = run(make_agent(tool_msg("get_status", {})).plan("how's the server", "r2"))
    assert isinstance(plan, Proposal) and not plan.needs_confirmation


def test_hallucinated_tool_is_refused():
    plan = run(make_agent(tool_msg("reboot_server", {})).plan("reboot", "r3"))
    assert isinstance(plan, Reply) and "can't" in plan.text


def test_week_long_pause_explains_cap():
    msg = tool_msg("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 168})
    plan = run(make_agent(msg).plan("pause transcoding for a week", "r4"))
    assert isinstance(plan, Reply) and "48 hours" in plan.text


def test_plain_answer_strips_think_tags():
    plan = run(make_agent({"content": "<think>hmm</think>\nI can't delete photos."}).plan("delete photos", "r5"))
    assert plan.text == "I can't delete photos."


def test_string_arguments_are_parsed():
    msg = {"tool_calls": [{"function": {"name": "restart_stack", "arguments": '{"name": "jellyfin"}'}}]}
    plan = run(make_agent(msg).plan("restart jellyfin", "r6"))
    assert isinstance(plan, Proposal) and plan.args == {"name": "jellyfin"}


# ---------------------------------------------------------------- execution + formatting
def test_execute_passes_request_id_and_formats():
    ex = FakeExecutor(body={"ok": True, "result": {"stack": "torrent", "steps": [
        {"container": "gluetun", "result": "healthy"}, {"container": "transmission", "result": "restarted"}]}})
    agent = make_agent(tool_msg("restart_stack", {"name": "torrent"}), ex)
    plan = run(agent.plan("restart torrent", "discord-42"))
    out = run(agent.execute(plan))
    assert ex.calls == [("restart_stack", {"name": "torrent"}, "discord-42")]
    assert "gluetun (healthy)" in out


def test_executor_rejection_is_reported():
    ex = FakeExecutor(400, {"ok": False, "error": "rejected", "detail": "unknown stack"})
    agent = make_agent(tool_msg("restart_stack", {"name": "torrent"}), ex)
    out = run(agent.execute(run(agent.plan("restart torrent", "r7"))))
    assert "refused" in out and "unknown stack" in out


def test_pause_result_shows_local_resume_time():
    out = format_result("immich_queue", {"queue": "videoConversion", "command": "pause",
                                         "resume_at": 1795457549.28}, TZ)
    assert "video transcoding" in out and "EST" in out


def test_status_format():
    out = format_status({"containers": {"gluetun": {"state": "running", "health": "healthy"},
                                        "caddy": {"state": "missing"}},
                         "host": {"uptime_hours": 32.1, "load": [0.27, 0.2, 0.1],
                                  "memory_gb": {"total": 14.5, "available": 10.3},
                                  "disks": [{"name": "root", "used_pct": 46.7, "free_gb": 484.1, "total_gb": 1003.8}],
                                  "temps_c": {"k10temp": 52.2}}})
    assert "🟢 gluetun: running" in out and "⚫ caddy: missing" in out and "47% used" in out


# ---------------------------------------------------------------- duration grounding
from steward_agent.tools import duration_hours_in


@pytest.mark.parametrize("text,hours", [
    ("pause immich video transcoding for 24 hours", 24),
    ("pause transcoding for a week", 168),
    ("pause immich video transcoding for 7 days", 168),
    ("pause it for two days", 48),
    ("pause for 36h", 36),
    ("pause for 1.5 hours", 1.5),
    ("pause video transcoding", None),
])
def test_duration_parser(text, hours):
    assert duration_hours_in(text) == hours


@pytest.mark.parametrize("text", ["pause immich video transcoding for a week",
                                  "pause immich video transcoding for 7 days"])
def test_model_invented_hours_cannot_dodge_cap(text):
    # Reproduces the live bug: model proposed 16 h for "a week".
    msg = tool_msg("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 16})
    plan = run(make_agent(msg).plan(text, "r8"))
    assert isinstance(plan, Reply) and "48 hours" in plan.text and "168" in plan.text


def test_user_duration_overrides_model_number():
    msg = tool_msg("immich_queue", {"action": "pause", "queue": "videoConversion", "hours": 12})
    plan = run(make_agent(msg).plan("pause transcoding for 2 days", "r9"))
    assert isinstance(plan, Proposal) and plan.args["hours"] == 48


def test_no_duration_keeps_model_value():
    msg = tool_msg("immich_queue", {"action": "pause", "queue": "videoConversion"})
    plan = run(make_agent(msg).plan("pause video transcoding", "r10"))
    assert isinstance(plan, Proposal) and "hours" not in plan.args


# ---------------------------------------------------------------- step 1 read-only tools
FULL = AgentConfig.model_validate({
    "stacks": {"torrent": {"description": "VPN", "restart_sequence": [{"container": "gluetun"},
                                                                     {"container": "transmission"}]}},
    "status": {"extra_containers": ["caddy"]},
    "integrations": {"immich": {"enabled": True, "queues": ["videoConversion"]},
                     "nextcloud": {"enabled": True}, "transmission": {"enabled": True},
                     "jellyfin": {"enabled": True}, "vpn": {"enabled": True}, "backup_log": {"enabled": True}},
})


def full_agent(message):
    return Agent(lambda: FULL, FakeLLM(message), FakeExecutor(), TZ)


def test_tool_count_stays_small():
    tools = build_tools(FULL)
    assert len(tools) == 8
    assert tools["get_status"].parameters["properties"]["topic"]["enum"] == ["overview", "backups", "vpn", "streaming"]
    assert tools["container_logs"].parameters["properties"]["container"]["enum"] == ["gluetun", "transmission", "caddy"]


@pytest.mark.parametrize("tool,args,verb,exec_args,confirm", [
    ("get_status", {}, "status", {}, False),
    ("get_status", {"topic": "backups"}, "backup_status", {}, False),
    ("get_status", {"topic": "streaming"}, "streams", {}, False),
    ("storage_usage", {"app": "nextcloud", "user": "bob"}, "storage_usage", {"app": "nextcloud", "user": "bob"}, False),
    ("torrents", {}, "torrent_list", {}, False),
    ("container_logs", {"container": "gluetun", "lines": "20"}, "container_logs", {"container": "gluetun", "lines": 20}, False),
    ("immich_queue", {"action": "status", "queue": "videoConversion"}, "immich_jobs", {}, False),
    ("immich_queue", {"action": "pause", "queue": "videoConversion"}, "immich_queue",
     {"action": "pause", "queue": "videoConversion"}, True),
])
def test_grouped_tools_route_to_executor_verbs(tool, args, verb, exec_args, confirm):
    plan = run(full_agent(tool_msg(tool, args)).plan("x", "r"))
    assert isinstance(plan, Proposal)
    assert (plan.verb, plan.exec_args, plan.needs_confirmation) == (verb, exec_args, confirm)


def test_pause_without_queue_asks_which():
    plan = run(full_agent(tool_msg("immich_queue", {"action": "pause"})).plan("pause immich", "r"))
    assert isinstance(plan, Reply) and "which queue" in plan.text


def test_logs_for_unlisted_container_refused():
    plan = run(full_agent(tool_msg("container_logs", {"container": "nextcloud-app-1"})).plan("logs", "r"))
    assert isinstance(plan, Reply)


def test_execute_uses_routed_verb():
    ex = FakeExecutor(body={"ok": True, "result": {"streams": []}})
    agent = Agent(lambda: FULL, FakeLLM(tool_msg("get_status", {"topic": "streaming"})), ex, TZ)
    out = run(agent.execute(run(agent.plan("who's watching", "r"))))
    assert ex.calls[0][0] == "streams" and out.startswith("Nobody is watching or listening")


def test_backup_format_shows_last_success_when_failed():
    from steward_agent.formatting import format_backups
    now = 1795500000.0
    out = format_backups({"last_run": {"outcome": "failed", "at": now - 3600, "message": "vault not mounted"},
                          "last_success": now - 9 * 86400, "repos_last_ok": {"vault": now - 9 * 86400},
                          "running_since": None}, TZ, now=now)
    assert "FAILED" in out and "vault not mounted" in out and "9 days ago" in out
    ok = format_backups({"last_run": {"outcome": "ok", "at": now - 7200}, "last_success": now - 7200,
                         "repos_last_ok": {}, "running_since": None}, TZ, now=now)
    assert ok.startswith("✅") and "Last successful" not in ok


def test_storage_and_logs_format():
    from steward_agent.formatting import format_logs, format_storage
    out = format_storage({"app": "immich", "total_bytes": 300e9, "user_count": 2, "users": [
        {"user": "alice", "bytes": 280e9, "photos": 45000, "videos": 2800},
        {"user": "bob", "bytes": 20e9, "photos": 5000, "videos": 200, "quota_bytes": 100e9}]})
    assert "280.0 GB" in out and "45,000 photos" in out and "20%" in out and "Total: 300.0 GB across 2" in out
    logs = format_logs({"container": "gluetun", "lines": ["x" * 290] * 50, "problem_lines_in_last_500": 3})
    assert len(logs) < 1900 and logs.endswith("```")


@pytest.mark.parametrize("user", ["", "all", "*", "All Users", "everyone", None, "  "])
def test_storage_everyone_placeholders_mean_no_user(user):
    """Live bug 2026-11-23: 'how much storage does each user use' sent a placeholder user."""
    plan = run(full_agent(tool_msg("storage_usage", {"app": "immich", "user": user})).plan("each user", "r"))
    assert isinstance(plan, Proposal) and plan.exec_args == {"app": "immich"}


def test_storage_real_user_kept():
    plan = run(full_agent(tool_msg("storage_usage", {"app": "nextcloud", "user": "Bob"})).plan("bob", "r"))
    assert plan.exec_args == {"app": "nextcloud", "user": "Bob"}


def test_streams_format_groups_apps_and_reports_errors():
    from steward_agent.formatting import format_streams
    out = format_streams({"apps": ["Jellyfin", "Audiobookshelf"], "errors": ["Jellyfin: HTTP 500"], "streams": [
        {"app": "audiobookshelf", "user": "bob", "title": "Dune", "client": "Abs Android",
         "where": "tailnet", "progress_pct": 40}]})
    assert "🎧 bob: Dune on Abs Android (tailnet, 40% through)" in out
    assert "⚠️ Couldn't check Jellyfin: HTTP 500" in out


def test_streaming_topic_lists_media_apps():
    from steward_agent.tools import build_tools
    cfg = AgentConfig.model_validate({"integrations": {"audiobookshelf": {"enabled": True}}})
    desc = build_tools(cfg)["get_status"].description
    assert "streaming" in desc and "Audiobookshelf" in desc and "Jellyfin" not in desc
