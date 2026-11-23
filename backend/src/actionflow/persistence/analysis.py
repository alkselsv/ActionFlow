from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from actionflow.persistence.database import Base, new_id, utc_now
from actionflow.persistence.project_settings import document


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        Index("ix_analysis_run_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    chat_id: Mapped[str | None] = mapped_column(ForeignKey("chat.id"))
    settings_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("settings_version.id")
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_to_queue: Mapped[bool]
    status: Mapped[str] = mapped_column(String(20), default="pending")
    stage: Mapped[str] = mapped_column(String(30), default="dispatch")
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(90), unique=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(36))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    limitations: Mapped[dict | None] = mapped_column(document)
    error_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None]
    detector_version: Mapped[str | None] = mapped_column(String(80))
    prompt_version: Mapped[str | None] = mapped_column(String(80))
    schema_version: Mapped[str | None] = mapped_column(String(80))
    provider: Mapped[str | None] = mapped_column(String(80))
    model_id: Mapped[str | None] = mapped_column(String(120))
    model_params: Mapped[dict | None] = mapped_column(document)
    usage: Mapped[dict | None] = mapped_column(document)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    candidate_count: Mapped[int] = mapped_column(default=0)
    finding_count: Mapped[int] = mapped_column(default=0)
    invalid_candidate_count: Mapped[int] = mapped_column(default=0)
    detector_duration_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisInputMessage(Base):
    __tablename__ = "analysis_input_message"

    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id"), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("message_revision.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(10))
    participant_side: Mapped[str] = mapped_column(String(10))
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (Index("ix_outbox_due", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    aggregate_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id"))
    event_key: Mapped[str] = mapped_column(String(36), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="start_analysis")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
