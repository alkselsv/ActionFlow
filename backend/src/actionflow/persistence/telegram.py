from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from actionflow.persistence.database import Base, utc_now


class TelegramConnection(Base):
    """Manual binding between one project chat and the test bot."""

    __tablename__ = "telegram_connection"

    chat_id: Mapped[str] = mapped_column(
        ForeignKey("chat.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))


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
    last_error: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
