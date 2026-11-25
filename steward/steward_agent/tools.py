"""Tool registry. Generated from steward.yaml so the model only ever sees real stack and
queue names, and every call the model makes is validated against the same schema before
it goes anywhere. The executor validates again on its side."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import jsonschema

from .config import AgentConfig

QUEUE_LABELS = {
    "videoConversion": "video transcoding",
    "thumbnailGeneration": "thumbnail generation",
    "smartSearch": "smart search (CLIP)",
    "faceDetection": "face detection",
    "facialRecognition": "facial recognition",
    "metadataExtraction": "metadata extraction",
    "ocr": "text recognition (OCR)",
    "library": "external library scans",
    "sidecar": "sidecar files",
}


class ToolError(Exception):
    """The model asked for something that isn't allowed. Message is shown to the user."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    risk: str                                   # "read" runs at once, "confirm" needs ✅
    verb: str                                   # executor verb (default route)
    describe: Callable[[dict], str]             # human-readable summary of the action
    # Grouped tools pick the executor verb (and risk) from their arguments.
    route: Callable[[dict], tuple[str, dict, str]] | None = None

    def resolve(self, args: dict) -> tuple[str, dict, str]:
        return self.route(args) if self.route else (self.verb, args, self.risk)

    def ollama_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _no_params() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def build_tools(cfg: AgentConfig) -> dict[str, ToolSpec]:
    tools: dict[str, ToolSpec] = {}
    integ = cfg.integrations

    # ---- get_status: one tool, several read-only topics -------------------------------
    topics = {"overview": ("status", "containers, disk space, memory, load and temperatures")}
    if integ.backup_log.enabled:
        topics["backups"] = ("backup_status", "whether the last backup worked, and when the last good one was")
    if integ.vpn.enabled:
        topics["vpn"] = ("vpn_status", "whether the VPN is up, with its public IP and location")
    media = [n for n, on in (("Jellyfin", integ.jellyfin.enabled),
                             ("Audiobookshelf", integ.audiobookshelf.enabled)) if on]
    if media:
        topics["streaming"] = ("streams", "who is watching or listening right now (" + ", ".join(media) + ")")
    tools["get_status"] = ToolSpec(
        name="get_status",
        description="Check on the server (read only). Topics: "
                    + "; ".join(f"{k} = {v[1]}" for k, v in topics.items()) + ".",
        parameters={"type": "object", "properties": {
            "topic": {"type": "string", "enum": list(topics), "description": "What to check (default overview)"}},
            "additionalProperties": False},
        risk="read", verb="status",
        describe=lambda a: f"check {a.get('topic', 'overview')}",
        route=lambda a: (topics[a.get("topic", "overview")][0], {}, "read"),
    )

    # ---- storage per user ---------------------------------------------------------------
    apps = [a for a, on in (("immich", integ.immich.enabled), ("nextcloud", integ.nextcloud.enabled)) if on]
    if apps:
        tools["storage_usage"] = ToolSpec(
            name="storage_usage",
            description="Show how much storage each user uses in an app (read only), or one user's usage. "
                        "Apps: " + ", ".join(f"{a} ({'photos' if a == 'immich' else 'files'})" for a in apps) + ".",
            parameters={"type": "object", "properties": {
                "app": {"type": "string", "enum": apps},
                "user": {"type": "string", "description": "A specific person's user name. "
                         "Leave this out entirely to list every user."}},
                "required": ["app"], "additionalProperties": False},
            risk="read", verb="storage_usage",
            describe=lambda a: f"show {a['app']} storage" + (f" for {a['user']}" if a.get("user") else ""),
        )

    # ---- torrents --------------------------------------------------------------------------
    if integ.transmission.enabled:
        tools["torrents"] = ToolSpec(
            name="torrents",
            description="Show torrent downloads (read only): progress, speed and time left.",
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list"], "description": "list (default)"}},
                "additionalProperties": False},
            risk="read", verb="torrent_list",
            describe=lambda a: "list torrents",
            route=lambda a: ("torrent_list", {}, "read"),
        )

    # ---- restart stacks --------------------------------------------------------------------
    if cfg.stacks:
        names = sorted(cfg.stacks)
        listing = "; ".join(f"{n}: {cfg.stacks[n].description or 'no description'}" for n in names)
        tools["restart_stack"] = ToolSpec(
            name="restart_stack",
            description=f"Restart one service stack. Available stacks: {listing}",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "enum": names, "description": "Stack to restart"}},
                "required": ["name"], "additionalProperties": False,
            },
            risk="confirm", verb="restart_stack",
            describe=lambda a: f"restart the **{a['name']}** stack"
            + (f" ({cfg.stacks[a['name']].description})" if cfg.stacks[a["name"]].description else ""),
        )

    # ---- container logs --------------------------------------------------------------------
    containers = cfg.watched_containers()
    if containers:
        tools["container_logs"] = ToolSpec(
            name="container_logs",
            description="Show the most recent log lines of one container (read only), to see what went wrong. "
                        "Secrets are redacted.",
            parameters={"type": "object", "properties": {
                "container": {"type": "string", "enum": containers},
                "lines": {"type": "integer", "minimum": 5, "maximum": 50, "description": "Default 30"}},
                "required": ["container"], "additionalProperties": False},
            risk="read", verb="container_logs",
            describe=lambda a: f"show logs for {a['container']}",
        )

    # ---- Immich queues -----------------------------------------------------------------------
    im = integ.immich
    if im.enabled and im.queues:
        labels = "; ".join(f"{q} = {QUEUE_LABELS.get(q, q)}" for q in im.queues)

        def describe_immich(a: dict) -> str:
            if a["action"] == "status":
                return "check Immich job queues"
            q = QUEUE_LABELS.get(a["queue"], a["queue"])
            if a["action"] == "resume":
                return f"resume Immich **{q}**"
            if "hours" in a:
                return f"pause Immich **{q}** for {a['hours']} h, then resume it automatically"
            return f"pause Immich **{q}** until you resume it"

        def route_immich(a: dict) -> tuple[str, dict, str]:
            if a["action"] == "status":
                return "immich_jobs", {}, "read"
            return "immich_queue", a, "confirm"

        tools["immich_queue"] = ToolSpec(
            name="immich_queue",
            description=(
                "Immich photo server background jobs. 'status' shows how much work each queue has (read only). "
                "'pause' or 'resume' one queue; with 'pause', 'hours' (1 to "
                f"{im.max_pause_hours}) resumes it automatically later. Queues: {labels}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "pause", "resume"]},
                    "queue": {"type": "string", "enum": list(im.queues), "description": "Needed for pause/resume"},
                    "hours": {"type": "integer", "minimum": 1, "maximum": im.max_pause_hours,
                              "description": "Only with pause: hours until automatic resume"},
                },
                "required": ["action"], "additionalProperties": False,
            },
            risk="confirm", verb="immich_queue", describe=describe_immich, route=route_immich,
        )

    tools["list_schedules"] = ToolSpec(
        name="list_schedules",
        description="List pending scheduled actions, such as automatic Immich queue resumes.",
        parameters=_no_params(), risk="read", verb="list_schedules",
        describe=lambda a: "list scheduled actions",
    )
    tools["reload_config"] = ToolSpec(
        name="reload_config",
        description="Reload the Server Steward configuration file after the owner edited it.",
        parameters=_no_params(), risk="read", verb="reload_config",
        describe=lambda a: "reload the configuration",
    )
    return tools


EVERYONE = {"", "all", "everyone", "everybody", "each", "each user", "every user", "all users",
            "users", "any", "none", "null", "*"}

_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_UNIT_HOURS = {"h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1,
               "d": 24, "day": 24, "days": 24, "w": 168, "wk": 168, "wks": 168, "week": 168, "weeks": 168}
_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?|" + "|".join(_WORD_NUM) + r")\s*-?\s*(" + "|".join(sorted(_UNIT_HOURS, key=len, reverse=True)) + r")\b",
    re.I)


def duration_hours_in(text: str) -> float | None:
    """Duration the user actually wrote ("24 hours", "7 days", "a week"), in hours.
    Used to ground the model's `hours` argument in the user's words."""
    m = _DURATION_RE.search(text)
    if not m:
        return None
    num, unit = m.group(1).lower(), m.group(2).lower()
    n = float(num) if num[0].isdigit() else _WORD_NUM[num]
    return n * _UNIT_HOURS[unit]


def _coerce(schema: dict, args: dict) -> dict:
    """Small models often send "24" or 24.0 for integers. Fix only unambiguous cases."""
    out = dict(args)
    for key, prop in schema.get("properties", {}).items():
        if key not in out or prop.get("type") != "integer":
            continue
        v = out[key]
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            out[key] = int(v.strip())
        elif isinstance(v, float) and v.is_integer():
            out[key] = int(v)
    return out


def validate_call(tools: dict[str, ToolSpec], name: Any, args: Any) -> tuple[ToolSpec, dict]:
    if not isinstance(name, str) or name not in tools:
        raise ToolError(f"there is no action called {name!r}")
    spec = tools[name]
    if args is None or args == "":
        args = {}
    if not isinstance(args, dict):
        raise ToolError("the action arguments were malformed")
    # Drop explicit nulls and empty strings for optional fields (common small-model habit).
    required = spec.parameters.get("required", [])
    args = {k: v for k, v in args.items()
            if k in required or not (v is None or (isinstance(v, str) and not v.strip()))}
    # "Everyone" placeholders mean "no specific user".
    if spec.name == "storage_usage" and isinstance(args.get("user"), str) \
            and args["user"].strip().lower().strip("*. ") in EVERYONE:
        args.pop("user")
    args = _coerce(spec.parameters, args)

    hours_schema = spec.parameters.get("properties", {}).get("hours")
    if hours_schema and isinstance(args.get("hours"), int) and args["hours"] > hours_schema["maximum"]:
        raise ToolError(f"I can pause for at most {hours_schema['maximum']} hours. "
                        f"Ask for {hours_schema['maximum']} or less, and pause again later if needed")
    if spec.name == "immich_queue":
        if args.get("action") == "resume" and "hours" in args:
            args.pop("hours")  # meaningless for resume; drop rather than fail
        if args.get("action") == "status":
            args = {"action": "status"}
        elif args.get("action") in ("pause", "resume") and "queue" not in args:
            raise ToolError("which queue? " + ", ".join(spec.parameters["properties"]["queue"]["enum"]))

    try:
        jsonschema.validate(args, spec.parameters)
    except jsonschema.ValidationError as exc:
        raise ToolError(f"invalid arguments for {name}: {exc.message}") from None
    return spec, args
