"""推理返回结构与持久记录的共同词汇；模型不得生成工作流状态。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    backend: Literal["local_account", "openai_compatible"] = "local_account"
    local_model: str = "openai-codex/gpt-6-astra"
    api_base_url: str = ""
    api_model: str = ""
    response_mode: Literal["prompt_json", "json_object", "json_schema"] = "prompt_json"
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)

    @field_validator("local_model")
    @classmethod
    def valid_local_model(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._/\-:@+]{1,256}", value) or "/" not in value:
            raise ValueError("OMP模型须为明确的provider/model，不允许空白或通配符")
        return value


class ModelReply(BaseModel):
    value: BaseModel
    requested_model: str
    actual_model: str
    backend: str
    usage: dict[str, Any] | None
    called_at: str
    text: str


class ModelBackend(Protocol):
    def complete_json(
        self, *, system: str, payload: dict[str, Any], schema: type[BaseModel]
    ) -> ModelReply: ...


class BriefContent(StrictModel):
    question: str = Field(min_length=1)
    population_or_object: str
    constraints: str
    available_resources: str
    validation_goal: str


class Clarification(StrictModel):
    brief: BriefContent
    queries: list[str] = Field(min_length=1, max_length=3)


class EvidenceContent(StrictModel):
    claim: str = Field(min_length=1)
    source_id: str
    page_number: int = Field(ge=1)
    quote: str = Field(min_length=1)
    conditions: str
    limitations: str
    ai_relation: Literal["supports", "contradicts", "context_only", "uncertain"]


class EvidenceOutput(StrictModel):
    cards: list[EvidenceContent] = Field(max_length=5)


class CandidateContent(StrictModel):
    candidate_id: str
    method: str
    evidence_ids: list[str]
    conditions: str
    feasibility: str
    minimal_validation: str
    objections: str


class CandidateOutput(StrictModel):
    candidates: list[CandidateContent] = Field(min_length=2, max_length=2)


class DiagnosticOutput(StrictModel):
    ok: Literal[True]
