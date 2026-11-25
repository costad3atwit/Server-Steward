"""Executor view of steward.yaml.

Top-level keys the executor doesn't use (llm, help, ...) are ignored, but every section the
executor does read is strict, so a typo fails loudly at startup instead of silently
widening or narrowing what the agent can do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_RE = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"
STACK_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RestartStep(Strict):
    container: str = Field(pattern=NAME_RE)
    wait_healthy: bool = False
    timeout_seconds: int = Field(default=120, ge=5, le=900)
    # Extra restart attempts if wait_healthy times out (e.g. a VPN client stuck on a bad
    # server picks a new one on restart).
    retries: int = Field(default=0, ge=0, le=3)


class Stack(Strict):
    description: str = ""
    restart_sequence: list[RestartStep] = Field(min_length=1)


class Disk(Strict):
    name: str = Field(pattern=STACK_RE)
    path: str


class StatusConfig(Strict):
    disks: list[Disk] = Field(default_factory=list)
    extra_containers: list[str] = Field(default_factory=list)

    @field_validator("extra_containers")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        import re

        for name in v:
            if not re.match(NAME_RE, name):
                raise ValueError(f"invalid container name: {name!r}")
        return v


class ImmichConfig(Strict):
    enabled: bool = False
    url: str = "http://immich_server:2283"
    api_style: Literal["jobs"] = "jobs"
    queues: list[str] = Field(default_factory=list)
    max_pause_hours: int = Field(default=48, ge=1, le=168)

    @field_validator("queues")
    @classmethod
    def _queues(cls, v: list[str]) -> list[str]:
        import re

        for q in v:
            if not re.match(r"^[a-zA-Z]{1,64}$", q):
                raise ValueError(f"invalid queue name: {q!r}")
        return v


class NextcloudConfig(Strict):
    """Per-user storage via `occ` inside the Nextcloud container (no admin password)."""
    enabled: bool = False
    container: str = Field(default="nextcloud-app-1", pattern=NAME_RE)


class TransmissionConfig(Strict):
    """Read torrent progress over RPC. Login (if any) comes from TRANSMISSION_USER/_PASS."""
    enabled: bool = False
    url: str = "http://gluetun:9091/transmission/rpc"


class JellyfinConfig(Strict):
    """Who is streaming. API key comes from JELLYFIN_API_KEY."""
    enabled: bool = False
    url: str = "http://jellyfin:8096"


class AudiobookshelfConfig(Strict):
    """Who is listening. API token comes from AUDIOBOOKSHELF_TOKEN (an admin user's token)."""
    enabled: bool = False
    url: str = "http://audiobookshelf:80"


class VpnConfig(Strict):
    """VPN public IP and location, read from Gluetun's own log (no control-server auth)."""
    enabled: bool = False
    container: str = Field(default="gluetun", pattern=NAME_RE)


class BackupLogConfig(Strict):
    """Backup results parsed from a log file. Lines must start with 'YYYY-MM-DD HH:MM:SS';
    the patterns say which lines mean started, finished, failed and per-repository OK."""
    enabled: bool = False
    path: str = "/host/backup/backup.log"
    started_pattern: str = r"===== backup started"
    finished_pattern: str = r"===== backup finished"
    failed_pattern: str = r"FAILED: (?P<message>.*)"
    repo_ok_pattern: str = r"(?P<repo>[\w-]+): backup OK"


class Integrations(Strict):
    immich: ImmichConfig = Field(default_factory=ImmichConfig)
    nextcloud: NextcloudConfig = Field(default_factory=NextcloudConfig)
    transmission: TransmissionConfig = Field(default_factory=TransmissionConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    audiobookshelf: AudiobookshelfConfig = Field(default_factory=AudiobookshelfConfig)
    vpn: VpnConfig = Field(default_factory=VpnConfig)
    backup_log: BackupLogConfig = Field(default_factory=BackupLogConfig)


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    stacks: dict[str, Stack] = Field(default_factory=dict)
    status: StatusConfig = Field(default_factory=StatusConfig)
    integrations: Integrations = Field(default_factory=Integrations)

    @field_validator("stacks")
    @classmethod
    def _stack_names(cls, v: dict[str, Stack]) -> dict[str, Stack]:
        import re

        for name in v:
            if not re.match(STACK_RE, name):
                raise ValueError(f"invalid stack name: {name!r} (lowercase, digits, - and _)")
        return v

    def watched_containers(self) -> list[str]:
        seen: dict[str, None] = {}
        for stack in self.stacks.values():
            for step in stack.restart_sequence:
                seen[step.container] = None
        for name in self.status.extra_containers:
            seen[name] = None
        return list(seen)


def load_config(path: str | Path) -> ExecutorConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("steward.yaml must be a mapping at the top level")
    return ExecutorConfig.model_validate(raw)
