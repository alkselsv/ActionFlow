from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from actionflow.persistence.database import Base, new_id, utc_now

document = JSON().with_variant(JSONB, "postgresql")


class Chat(Base):
    __tablename__ = "chat"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), unique=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))


class Participant(Base):
    __tablename__ = "participant"
    __table_args__ = (UniqueConstraint("chat_id", "source_author_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chat.id"))
    source_author_id: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str | None] = mapped_column(String(200))


class SettingsVersion(Base):
    __tablename__ = "settings_version"
    __table_args__ = (UniqueConstraint("project_id", "version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"))
    version_no: Mapped[int]
    context_text: Mapped[str]
    agreements: Mapped[str]
    manager_name: Mapped[str] = mapped_column(String(200))
    client_type: Mapped[str] = mapped_column(String(20))
    work_hours: Mapped[dict] = mapped_column(document)
    timezone: Mapped[str] = mapped_column(String(100))
    first_response_minutes: Mapped[int]
    calendar: Mapped[dict] = mapped_column(document)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    sides: Mapped[list[ParticipantSide]] = relationship(
        order_by="ParticipantSide.source_author_id"
    )


class ParticipantSide(Base):
    __tablename__ = "participant_side"

    settings_version_id: Mapped[str] = mapped_column(
        ForeignKey("settings_version.id"), primary_key=True
    )
    source_author_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    side: Mapped[str] = mapped_column(String(10))


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("chat_id", "external_id"),
        Index("ix_message_chat_sent", "chat_id", "sent_at", "external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chat.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    participant_id: Mapped[str | None] = mapped_column(ForeignKey("participant.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    text: Mapped[str] = mapped_column(String, default="")
    text_format: Mapped[list] = mapped_column(document, default=list)
    reply_to_external_id: Mapped[str | None] = mapped_column(String(200))
    participant: Mapped[Participant | None] = relationship()


class MessageRevision(Base):
    __tablename__ = "message_revision"
    __table_args__ = (UniqueConstraint("message_id", "revision_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("message.id"))
    revision_no: Mapped[int]
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict] = mapped_column(document)
    is_current: Mapped[bool]
    ordering_uncertain: Mapped[bool]
    message: Mapped[Message] = relationship()


class ImportReceipt(Base):
    __tablename__ = "import_receipt"

    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict] = mapped_column(document)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceObservation(Base):
    __tablename__ = "source_observation"
    __table_args__ = (
        Index("ix_source_observation_revision_recorded", "revision_id", "recorded_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("message_revision.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[MessageRevision] = relationship()
