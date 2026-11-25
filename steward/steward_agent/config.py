"""Agent view of steward.yaml. Lenient on purpose: the executor is the strict validator,
the agent only needs names and descriptions to build tool schemas and prompts."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class LLMConfig(Loose):
    chat_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    embed_model: str = "nomic-embed-text"
    temperature: float = Field(default=0.2, ge=0, le=2)
    options: dict = Field(default_factory=dict)


class StepInfo(Loose):
    container: str


class StackInfo(Loose):
    description: str = ""
    restart_sequence: list[StepInfo] = Field(default_factory=list)


class StatusInfo(Loose):
    extra_containers: list[str] = Field(default_factory=list)


class Toggle(Loose):
    enabled: bool = False


class ImmichInfo(Loose):
    enabled: bool = False
    queues: list[str] = Field(default_factory=list)
    max_pause_hours: int = 48


class Integrations(Loose):
    immich: ImmichInfo = Field(default_factory=ImmichInfo)
    nextcloud: Toggle = Field(default_factory=Toggle)
    transmission: Toggle = Field(default_factory=Toggle)
    jellyfin: Toggle = Field(default_factory=Toggle)
    audiobookshelf: Toggle = Field(default_factory=Toggle)
    vpn: Toggle = Field(default_factory=Toggle)
    backup_log: Toggle = Field(default_factory=Toggle)


class RagConfig(Loose):
    top_k: int = Field(default=4, ge=1, le=10)
    # Cosine similarity a chunk needs to count as relevant. Below it, the help page
    # forwards the question instead of answering. Tune with: steward-cli search help "..."
    min_score: float = Field(default=0.6, ge=0, le=1)
    # nomic-embed-text expects these task prefixes; set both to "" for other models.
    doc_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
    # Run the embedding model on the CPU so it doesn't evict the chat model from a small GPU.
    embed_options: dict = Field(default_factory=lambda: {"num_gpu": 0})


class HelpConfig(Loose):
    enabled: bool = True
    # How the help page refers to the person who answers forwarded questions.
    owner_name: str = "the server admin"
    max_question_chars: int = Field(default=500, ge=50, le=4000)
    rate_limit_per_minute: int = Field(default=10, ge=1, le=600)
    forward_unanswered: bool = True
    max_forwards_per_hour: int = Field(default=20, ge=0, le=1000)
    max_answer_tokens: int = Field(default=400, ge=50, le=2000)
    # POST /message: a plain "message the admin" form relayed to Discord (no AI involved).
    contact_form: bool = True
    contact_messages_per_hour: int = Field(default=5, ge=1, le=100)   # per client IP


class AgentConfig(Loose):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    help: HelpConfig = Field(default_factory=HelpConfig)
    confirm_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    stacks: dict[str, StackInfo] = Field(default_factory=dict)
    status: StatusInfo = Field(default_factory=StatusInfo)
    integrations: Integrations = Field(default_factory=Integrations)

    def watched_containers(self) -> list[str]:
        seen: dict[str, None] = {}
        for stack in self.stacks.values():
            for step in stack.restart_sequence:
                seen[step.container] = None
        for name in self.status.extra_containers:
            seen[name] = None
        return list(seen)


def load_config(path: str | Path) -> AgentConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AgentConfig.model_validate(raw)
