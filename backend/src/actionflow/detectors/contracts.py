"""Contracts shared by replaceable answer detectors."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class DetectorError(Exception):
    """Base class for errors that an activity must translate to Temporal errors."""


class DetectorRateLimited(DetectorError):
    pass


class DetectorTimeout(DetectorError):
    pass


class DetectorPermanent(DetectorError):
    pass


class InvalidDetectorOutput(DetectorError):
    pass


@dataclass(frozen=True)
class InputMessage:
    message_id: str
    revision_id: str
    external_id: str
    text: str
    sent_at: datetime
    role: str
    side: str
    source_type: str
    reply_to_external_id: str | None
    attachments: dict
    author_name: str | None


@dataclass(frozen=True)
class DetectorRequest:
    run_id: str
    project_id: str
    period_start: datetime
    period_end: datetime
    as_of: datetime
    messages: tuple[InputMessage, ...]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["anchor", "supporting", "context"]
    message_id: str = Field(min_length=1, max_length=36)
    revision_id: str = Field(min_length=1, max_length=36)
    quote: str | None = Field(default=None, max_length=2000)


class Subject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["message", "issue", "commitment", "complaint_episode"]
    anchor_message_id: str = Field(min_length=1, max_length=36)
    anchor_revision_id: str = Field(min_length=1, max_length=36)
    fragment: str | None = Field(default=None, max_length=1000)
    discriminator: str | None = Field(default=None, max_length=200)


class CommitmentTerms(BaseModel):
    """Structured commitment facts detected in one promise or update."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["promise", "repeat", "reschedule", "cancel", "fulfill"] = (
        "promise"
    )
    subject_text: str | None = Field(default=None, max_length=2000)
    due_expression: str | None = Field(default=None, max_length=500)
    assignee_name: str | None = Field(default=None, max_length=200)
    explicit_risk: bool = False


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str = Field(min_length=1, max_length=128)
    subject: Subject
    description: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    priority_hint: Literal["low", "medium", "high", "unknown"] = "unknown"
    suggested_action: Literal["reply", "review", "follow_up", "none"] = "review"
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    commitment: CommitmentTerms | None = None
    limitations: list[
        Literal[
            "first_reaction_without_resolution",
            "no_vedita_response",
            "possible_attachment_answer",
            "unparsed_content",
            "unknown_side",
            "missing_text",
            "history_incomplete",
            "system_events",
            "ambiguous_due",
            "ambiguous_update",
        ]
    ] = Field(default_factory=list, max_length=20)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing_version: str | None = Field(default=None, max_length=80)


class DetectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["detector_result.v1"] = "detector_result.v1"
    finding_type: Literal["unanswered", "commitment", "complaint"]
    candidates: list[Candidate] = Field(max_length=10_000)
    usage: Usage = Field(default_factory=Usage)
    diagnostics: list[str] = Field(default_factory=list, max_length=100)


class Detector(Protocol):
    """A replaceable model adapter. It may be local or a remote LLM."""

    provider: str
    model_id: str
    detector_version: str
    prompt_version: str
    schema_version: str
    model_params: dict

    def detect(self, request: DetectorRequest) -> DetectorResult: ...
