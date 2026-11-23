from datetime import timedelta

from sqlalchemy import select

from actionflow.persistence.database import as_utc
from actionflow.persistence.telegram import TelegramConnection, TelegramGap

RETENTION = timedelta(hours=24)


def open_gap(db, connection, reason, started, ended=None):
    started = max(started, as_utc(connection.connected_at))
    if ended is not None and ended <= started:
        return
    if (
        ended is None
        and db.scalar(
            select(TelegramGap.id)
            .where(
                TelegramGap.chat_id == connection.chat_id,
                TelegramGap.reason == reason,
                TelegramGap.end_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        return
    db.add(
        TelegramGap(
            chat_id=connection.chat_id, reason=reason, start_at=started, end_at=ended
        )
    )


def close_gap(db, connection, reason, ended):
    for gap in db.scalars(
        select(TelegramGap).where(
            TelegramGap.chat_id == connection.chat_id,
            TelegramGap.reason == reason,
            TelegramGap.end_at.is_(None),
        )
    ):
        gap.end_at = max(as_utc(gap.start_at), ended)


def record_retention_gap(db, last_poll, now):
    if last_poll is not None and now - last_poll <= RETENTION:
        return
    for connection in db.scalars(
        select(TelegramConnection).where(TelegramConnection.status != "disconnected")
    ):
        open_gap(
            db,
            connection,
            "retention_expired",
            last_poll or as_utc(connection.connected_at),
            now - RETENTION,
        )
