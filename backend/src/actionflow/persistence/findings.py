from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from actionflow.persistence.database import Base, new_id, utc_now
from actionflow.persistence.project_settings import document


class Candidate(Base):
    """One model answer for one subject in one immutable analysis run."""

    __tablename__ = "candidate"
    __table_args__ = (
        UniqueConstraint("run_id", "candidate_key"),
        Index("ix_candidate_run", "run_id"),
        Index("ix_candidate_project_type", "project_id", "finding_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    finding_type: Mapped[str] = mapped_column(String(20))
    candidate_key: Mapped[str] = mapped_column(String(128))
    subject_kind: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36))
    anchor_message_id: Mapped[str] = mapped_column(ForeignKey("message.id"))
    anchor_revision_id: Mapped[str] = mapped_column(
        ForeignKey("message_revision.id")
    )
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    priority_hint: Mapped[str] = mapped_column(String(20), default="unknown")
    suggested_action: Mapped[str] = mapped_column(String(30), default="review")
    limitations: Mapped[list] = mapped_column(document, default=list)
    validation_status: Mapped[str] = mapped_column(String(20), default="valid")
    validation_errors: Mapped[list] = mapped_column(document, default=list)
    raw_payload: Mapped[dict] = mapped_column(document)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class CandidateEvidence(Base):
    __tablename__ = "candidate_evidence"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "role", "message_id", "revision_id", "quote_hash"
        ),
        Index("ix_candidate_evidence_candidate", "candidate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"))
    role: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("message_revision.id"))
    quote: Mapped[str | None] = mapped_column(Text)
    quote_hash: Mapped[str | None] = mapped_column(String(64))


class Commitment(Base):
    """A durable Vedita promise with its own independently tracked state."""

    __tablename__ = "commitment"
    __table_args__ = (
        UniqueConstraint("project_id", "anchor_message_id"),
        Index("ix_commitment_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    anchor_message_id: Mapped[str] = mapped_column(ForeignKey("message.id"))
    anchor_revision_id: Mapped[str] = mapped_column(
        ForeignKey("message_revision.id")
    )
    subject_text: Mapped[str] = mapped_column(Text)
    assignee_name: Mapped[str | None] = mapped_column(String(200))
    assignee_participant_id: Mapped[str | None] = mapped_column(
        ForeignKey("participant.id")
    )
    status: Mapped[str] = mapped_column(String(20), default="open")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    explicit_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class CommitmentEvent(Base):
    """Immutable evidence for a promise, repeat, reschedule, cancellation or fulfillment."""

    __tablename__ = "commitment_event"
    __table_args__ = (
        UniqueConstraint(
            "commitment_id", "kind", "message_id", "revision_id"
        ),
        Index("ix_commitment_event_commitment_at", "commitment_id", "at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitment.id"))
    kind: Mapped[str] = mapped_column(String(20))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("message.id"))
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("message_revision.id")
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class Finding(Base):
    """A durable manager-facing card. Its identity is the observed subject."""

    __tablename__ = "finding"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "finding_type", "subject_kind", "subject_id"
        ),
        Index(
            "ix_finding_queue",
            "project_id",
            "status",
            "priority",
            "updated_at",
        ),
        Index("ix_finding_sla", "project_id", "sla_state", "priority"),
        Index(
            "ix_finding_commitment",
            "project_id",
            "finding_type",
            "commitment_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    finding_type: Mapped[str] = mapped_column(String(20))
    subject_kind: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(36))
    anchor_message_id: Mapped[str] = mapped_column(ForeignKey("message.id"))
    anchor_revision_id: Mapped[str] = mapped_column(
        ForeignKey("message_revision.id")
    )
    status: Mapped[str] = mapped_column(String(20), default="new")
    priority: Mapped[str] = mapped_column(String(20), default="unknown")
    priority_reason: Mapped[dict] = mapped_column(document, default=dict)
    description: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[str] = mapped_column(String(30), default="review")
    confidence: Mapped[float] = mapped_column(Float)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_state: Mapped[str] = mapped_column(String(30), default="not_calculated")
    sla_reason: Mapped[dict | None] = mapped_column(document)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    commitment_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    commitment_status: Mapped[str | None] = mapped_column(String(20))
    latest_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate.id")
    )
    current_run_id: Mapped[str | None] = mapped_column(ForeignKey("analysis_run.id"))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class FindingObservation(Base):
    __tablename__ = "finding_observation"
    __table_args__ = (
        UniqueConstraint("finding_id", "run_id"),
        Index(
            "ix_finding_observation_finding_observed",
            "finding_id",
            "observed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    finding_id: Mapped[str] = mapped_column(ForeignKey("finding.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("analysis_run.id"))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(20))
    priority_reason: Mapped[dict | None] = mapped_column(document)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sla_state: Mapped[str | None] = mapped_column(String(30))
    sla_reason: Mapped[dict | None] = mapped_column(document)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    commitment_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    commitment_status: Mapped[str | None] = mapped_column(String(20))
    settings_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "settings_version.id", name="fk_finding_observation_settings_version"
        )
    )
