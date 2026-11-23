"""Normalize Bot API updates into the existing message model.

This module is transport-independent for tests: it accepts already fetched
Telegram updates and never calls the Bot API. The caller owns the database
transaction. The update cursor is advanced in that same transaction only after
all accepted events have been written, so a failed commit is retried instead of
acknowledged.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from actionflow.api.telegram_import import add_observation
from actionflow.persistence.database import as_utc, new_id, utc_now
from actionflow.persistence.project_settings import (
    Chat,
    Message,
    MessageRevision,
    Participant,
)
from actionflow.persistence.telegram import TelegramBotState, TelegramConnection

ALLOWED_UPDATES = ("message", "edited_message", "my_chat_member")
SOURCE_KIND = "telegram_bot"

MEDIA_FIELDS = {
    "photo": "photo",
    "document": "file",
    "voice": "voice_message",
    "video": "video_file",
    "video_note": "video_message",
    "sticker": "sticker",
    "audio": "audio_file",
    "animation": "animation",
}

SERVICE_FIELDS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "invoice",
    "successful_payment",
    "users_shared",
    "chat_shared",
    "connected_website",
    "write_access_allowed",
    "passport_data",
    "proximity_alert_triggered",
    "forum_topic_created",
    "forum_topic_closed",
    "forum_topic_reopened",
    "forum_topic_edited",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "web_app_data",
    "giveaway_created",
    "giveaway_winners",
    "giveaway_completed",
)

MEMBER_STATUSES = {"member", "administrator", "creator"}
LEFT_STATUSES = {"left", "kicked"}


@dataclass
class IngestReport:
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    messages_created: int = 0
    revisions_created: int = 0
    unchanged: int = 0
    non_text: int = 0
    service_events: int = 0
    last_update_id: int | None = None
    rejected_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.rejected_reasons[reason] = self.rejected_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "messages_created": self.messages_created,
            "revisions_created": self.revisions_created,
            "unchanged": self.unchanged,
            "non_text": self.non_text,
            "service_events": self.service_events,
            "last_update_id": self.last_update_id,
            "rejected_reasons": dict(self.rejected_reasons),
        }


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.lstrip("-").isdigit():
            return int(value)
    return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str):
        value = value.strip()
        if 0 < len(value) <= 200:
            return value
    return None


def _timestamp(value: Any) -> datetime | None:
    seconds = _integer(value)
    if seconds is None or seconds < 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _chat_id(message: dict[str, Any]) -> str | None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    return _identifier(chat.get("id"))


def _message_id(message: dict[str, Any]) -> str | None:
    return _identifier(message.get("message_id"))


def _display_name(person: Any) -> str | None:
    if not isinstance(person, dict):
        return None
    parts = [
        part.strip()
        for part in (person.get("first_name"), person.get("last_name"))
        if isinstance(part, str) and part.strip()
    ]
    if not parts and isinstance(person.get("username"), str):
        username = person["username"].strip()
        if username:
            return username[:200]
    return " ".join(parts)[:200] or None


def _formatting(message: dict[str, Any], *, caption: bool = False) -> list[dict]:
    key = "caption_entities" if caption else "entities"
    value = message.get(key)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _attachments(message: dict[str, Any]) -> dict[str, Any]:
    for field_name, media_type in MEDIA_FIELDS.items():
        value = message.get(field_name)
        if not value:
            continue
        metadata: dict[str, Any] = {"media_type": media_type, field_name: True}
        item = value[0] if isinstance(value, list) and value else value
        if isinstance(item, dict):
            for key in ("file_name", "mime_type", "duration", "width", "height"):
                if item.get(key) is not None:
                    metadata[key] = item[key]
        return metadata
    return {}


def _service_action(message: dict[str, Any]) -> str | None:
    for field_name in SERVICE_FIELDS:
        if field_name in message:
            return field_name
    return None


def _participant(db, chat: Chat, author_id: str, name: str | None) -> Participant:
    participant = db.scalar(
        select(Participant).where(
            Participant.chat_id == chat.id,
            Participant.source_author_id == author_id,
        )
    )
    if participant is None:
        participant = Participant(
            id=new_id(),
            chat_id=chat.id,
            source_author_id=author_id,
            display_name=name,
        )
        db.add(participant)
    elif not participant.display_name and name:
        participant.display_name = name
    return participant


def _snapshot(db, chat: Chat, message: dict[str, Any], update_id: int, edited: bool):
    message_id = _message_id(message)
    sent_at = _timestamp(message.get("date"))
    if message_id is None or sent_at is None:
        return None, "malformed_message"

    service_action = _service_action(message)
    sender = message.get("from")
    author_id = _identifier(sender.get("id")) if isinstance(sender, dict) else None

    text = message.get("text")
    caption = message.get("caption")
    using_caption = not isinstance(text, str) and isinstance(caption, str)
    if not isinstance(text, str):
        text = caption if isinstance(caption, str) else ""
    formatting = _formatting(message, caption=using_caption)
    attachments = _attachments(message)
    if service_action is None and not text and not attachments:
        return None, "empty_message"

    participant = None
    actor_id = None
    actor_name = None
    if service_action is None and author_id:
        participant = _participant(db, chat, author_id, _display_name(sender))
    if service_action is not None:
        actor_id = author_id
        actor_name = _display_name(sender)

    reply_to = None
    reply_message = message.get("reply_to_message")
    if isinstance(reply_message, dict):
        reply_to = _message_id(reply_message)

    edited_at = _timestamp(message.get("edit_date")) if edited else None
    if edited and edited_at is None:
        edited_at = utc_now()

    snapshot = {
        "text": text,
        "text_format": formatting,
        "reply_to_external_id": reply_to,
        "participant_id": participant.id if participant else None,
        "attachments": attachments,
        "source_type": "service" if service_action else "message",
        "service_action": service_action,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "source_date": sent_at.isoformat(),
        "source_kind": SOURCE_KIND,
        "source_update_id": update_id,
        "source_revision": None,
        "source_payload": {
            "update_id": update_id,
            "event": "edited_message" if edited else "message",
        },
    }
    return (message_id, sent_at, edited_at, snapshot), None


def _store_event(
    db,
    chat: Chat,
    message_id: str,
    sent_at: datetime,
    edited_at: datetime | None,
    snapshot: dict[str, Any],
    report: IngestReport,
    now: datetime,
) -> None:
    message = db.scalar(
        select(Message).where(
            Message.chat_id == chat.id, Message.external_id == message_id
        )
    )
    created = message is None
    if message is None:
        message = Message(
            id=new_id(),
            chat_id=chat.id,
            external_id=message_id,
            sent_at=sent_at,
            observed_at=now,
        )
        db.add(message)
    else:
        message.observed_at = now
    versions = list(
        db.scalars(
            select(MessageRevision)
            .where(MessageRevision.message_id == message.id)
            .order_by(MessageRevision.revision_no)
        )
    )
    observation, _changed, _uncertain = add_observation(
        db, message, versions, snapshot, edited_at, None, now
    )
    if created:
        report.messages_created += 1
    elif observation:
        report.revisions_created += 1
    else:
        report.unchanged += 1
    report.non_text += int(bool(snapshot.get("attachments")))
    report.service_events += int(snapshot.get("source_type") == "service")


def _connection(db, chat_id: str):
    return db.execute(
        select(TelegramConnection, Chat)
        .join(Chat, Chat.id == TelegramConnection.chat_id)
        .where(
            Chat.source == "telegram",
            Chat.external_id == chat_id,
        )
    ).first()


def _membership_status(update: dict[str, Any]) -> str | None:
    change = update.get("my_chat_member")
    if not isinstance(change, dict):
        return None
    member = change.get("new_chat_member")
    return member.get("status") if isinstance(member, dict) else None


def ingest_updates(
    db,
    updates: Any,
    *,
    bot_id: str,
    username: str | None = None,
    now: datetime | None = None,
) -> IngestReport:
    """Write a batch and move the bot cursor inside the caller's transaction."""
    if not bot_id:
        raise ValueError("bot_id is required")
    now = now or utc_now()
    report = IngestReport()
    state = db.get(TelegramBotState, bot_id)
    if state is None:
        state = TelegramBotState(bot_id=bot_id, updated_at=now)
        db.add(state)
    previous_update_id = state.last_update_id
    maximum_update_id = previous_update_id
    seen_update_ids = set()

    if updates is None:
        updates = []
    if not isinstance(updates, (list, tuple)):
        raise TypeError("updates must be a list")

    for raw in updates:
        report.received += 1
        if not isinstance(raw, dict):
            report.reject("malformed_update")
            continue
        update_id = _integer(raw.get("update_id"))
        if update_id is None:
            report.reject("missing_update_id")
            continue
        if (
            (previous_update_id is not None and update_id <= previous_update_id)
            or update_id in seen_update_ids
        ):
            report.reject("duplicate_or_stale_update")
            continue
        seen_update_ids.add(update_id)
        maximum_update_id = (
            update_id
            if maximum_update_id is None
            else max(maximum_update_id, update_id)
        )

        event = next(
            (name for name in ALLOWED_UPDATES if isinstance(raw.get(name), dict)),
            None,
        )
        if event is None:
            report.reject("unsupported_update")
            continue
        payload = raw[event]
        chat_external_id = _chat_id(payload)
        if chat_external_id is None:
            report.reject("malformed_update")
            continue
        row = _connection(db, chat_external_id)
        if row is None:
            report.reject("unknown_chat")
            continue
        connection, chat = row
        if connection.status == "disconnected":
            report.reject("inactive_connection")
            continue

        if event == "my_chat_member":
            status = _membership_status(raw)
            if status in MEMBER_STATUSES:
                connection.status = "active"
                connection.last_error = None
            elif status in LEFT_STATUSES:
                connection.status = "needs_check"
                connection.last_error = "bot_left"
            else:
                connection.status = "needs_check"
                connection.last_error = "invalid_membership"
                connection.updated_at = now
                report.reject("invalid_membership")
                continue
            connection.updated_at = now
            report.accepted += 1
            continue

        normalized, reason = _snapshot(db, chat, payload, update_id, event == "edited_message")
        if normalized is None:
            connection.status = "needs_check"
            connection.last_error = str(reason)[:100]
            connection.updated_at = now
            report.reject(str(reason))
            continue
        message_id, sent_at, edited_at, snapshot = normalized
        _store_event(
            db,
            chat,
            message_id,
            sent_at,
            edited_at,
            snapshot,
            report,
            now,
        )
        previous_event_at = as_utc(connection.last_event_at)
        connection.last_event_at = max(previous_event_at or sent_at, sent_at)
        connection.updated_at = now
        report.accepted += 1

    if maximum_update_id is not None and (
        state.last_update_id is None or maximum_update_id > state.last_update_id
    ):
        state.last_update_id = maximum_update_id
    if username:
        state.username = username[:200]
    state.updated_at = now
    db.flush()
    report.last_update_id = state.last_update_id
    return report
