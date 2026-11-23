from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from actionflow.persistence.database import Base, utc_now


class TelegramConnection(Base):
    """Manual binding between one project chat and the test bot."""

    __tablename__ = "telegram_connection"

    chat_id: Mapped[str] = mapped_column(ForeignKey("chat.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_membership_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_membership_update_id: Mapped[int | None] = mapped_column(BigInteger)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramBotState(Base):
    """Per-bot update cursor and last polling outcome.

    The cursor is stored only after all events from a batch were written. This
    makes the Telegram offset an acknowledgement of a database commit.
    """

    __tablename__ = "telegram_bot_state"

    bot_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(200))
    last_update_id: Mapped[int | None] = mapped_column(BigInteger)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_caught_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipt"

    bot_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)


class TelegramGap(Base):
    __tablename__ = "telegram_gap"
    __table_args__ = (Index("ix_telegram_gap_chat", "chat_id", "start_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("telegram_connection.chat_id"))
    reason: Mapped[str] = mapped_column(String(30))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
