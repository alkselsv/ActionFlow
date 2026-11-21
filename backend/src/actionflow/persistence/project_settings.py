from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
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
