import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import select

from actionflow.persistence.database import new_id, utc_now
from actionflow.persistence.project_settings import (
    Chat,
    ImportReceipt,
    Message,
    MessageRevision,
    Participant,
    SourceObservation,
)

MAX_BYTES = 50 * 1024 * 1024
MEDIA_FIELDS = (
    "photo",
    "file",
    "voice_message",
    "video_message",
    "video_file",
    "sticker",
    "audio_file",
)


def as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def unix_time(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except ValueError, OverflowError, OSError:
        return None


def source_time(value):
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.astimezone(UTC) if dt.tzinfo else None
        except ValueError, OverflowError:
            pass
    return None


def source_label(value):
    return value if isinstance(value, str) else None


def external_id(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = str(value).strip()
        if 0 < len(value) <= 200:
            return value
    return None


def source_metadata(item):
    meta = item.get("_actionflow_synthetic_source")
    return meta if isinstance(meta, dict) else {}


def normalized_text(value):
    if isinstance(value, str):
        return value, []
    if not isinstance(value, list):
        return "", []
    formatting = [part for part in value if isinstance(part, dict)]
    text = "".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in value
        if isinstance(part, (str, dict))
    )
    return text, formatting


def attachment_metadata(item):
    files = {field: item[field] for field in MEDIA_FIELDS if item.get(field)}
    if not files and not item.get("media_type"):
        return {}
    return {
        **files,
        **{
            key: item[key]
            for key in (
                "media_type",
                "mime_type",
                "duration_seconds",
                "width",
                "height",
            )
            if key in item
        },
    }


def revision_result(revision):
    snapshot = revision.snapshot
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "text": snapshot["text"],
        "text_format": snapshot["text_format"],
        "reply_to_external_id": snapshot["reply_to_external_id"],
        "source_type": snapshot.get("source_type", "message"),
        "service_action": snapshot.get("service_action"),
        "actor_id": snapshot.get("actor_id"),
        "actor_name": snapshot.get("actor_name"),
        "attachments": snapshot.get("attachments", {}),
        "source_date": snapshot.get("source_date"),
        "edited_at": as_utc(revision.edited_at),
        "observed_at": as_utc(revision.observed_at),
        "source_observed_at": as_utc(revision.source_observed_at),
        "ordering_uncertain": revision.ordering_uncertain,
    }


def content_signature(snapshot):
    return (
        snapshot["text"],
        snapshot["text_format"],
        snapshot["reply_to_external_id"],
        snapshot.get("participant_id"),
        snapshot.get("source_type", "message"),
        snapshot.get("service_action"),
        snapshot.get("actor_id"),
        snapshot.get("actor_name"),
        snapshot.get("attachments", {}),
    )


def add_observation(db, message, versions, snapshot, edited, source_observed, now):
    active = next(
        (version for version in reversed(versions) if version.is_current), None
    )
    previous_edit = as_utc(active.edited_at) if active else None
    signature = content_signature(snapshot)
    source_revision = snapshot.get("source_revision")
    active_source_revision = active.snapshot.get("source_revision") if active else None
    comparable = source_revision is not None and active_source_revision is not None
    if (
        active
        and edited == previous_edit
        and (not comparable or source_revision == active_source_revision)
        and signature == content_signature(active.snapshot)
    ):
        known_source = as_utc(active.source_observed_at)
        if source_observed is not None and (
            known_source is None or source_observed < known_source
        ):
            active.source_observed_at = source_observed
            db.add(
                SourceObservation(
                    revision=active, recorded_at=now, source_observed_at=source_observed
                )
            )
        return False, False, False
    is_newer = active is None or (
        edited is not None and edited > (previous_edit or as_utc(message.sent_at))
    )
    if comparable:
        is_newer = source_revision > active_source_revision
    if not is_newer and any(
        edited == as_utc(version.edited_at)
        and signature == content_signature(version.snapshot)
        for version in versions
    ):
        return False, False, False
    uncertain = bool(
        active
        and not is_newer
        and (
            source_revision == active_source_revision
            if comparable
            else edited is None or previous_edit is None or edited == previous_edit
        )
    )
    revision = MessageRevision(
        id=new_id(),
        message_id=message.id,
        revision_no=len(versions) + 1,
        edited_at=edited,
        source_observed_at=source_observed,
        observed_at=now,
        snapshot=snapshot,
        is_current=is_newer,
        ordering_uncertain=uncertain,
    )
    db.add(revision)
    if source_observed is not None:
        db.add(
            SourceObservation(
                revision=revision, recorded_at=now, source_observed_at=source_observed
            )
        )
    versions.append(revision)
    if is_newer:
        if active:
            active.is_current = False
        for field in ("text", "text_format", "reply_to_external_id", "participant_id"):
            setattr(message, field, snapshot[field])
    return True, bool(active and is_newer), uncertain


def import_chat(db, project, payload, key=None):
    if len(payload) > MAX_BYTES:
        raise HTTPException(413, "Максимальный размер экспорта — 50 МБ")
    now = utc_now()
    digest = sha256(payload).hexdigest() if key else None
    receipt = db.get(ImportReceipt, (project.id, key)) if key else None
    if receipt and as_utc(receipt.created_at) + timedelta(hours=24) > now:
        if receipt.payload_hash != digest:
            raise HTTPException(
                409,
                {
                    "code": "idempotency_conflict",
                    "message": "Ключ повтора уже использован для другого файла",
                },
            )
        return receipt.report, True
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except UnicodeDecodeError, ValueError, RecursionError:
        raise HTTPException(400, "Файл не является корректным UTF-8 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise HTTPException(422, "Ожидается экспорт Telegram с массивом messages")
    records = data["messages"]
    if len(records) > 200_000:
        raise HTTPException(413, "Максимум 200 000 записей за импорт")
    chat_id = external_id(data.get("id"))
    if not chat_id or not isinstance(data.get("name"), str):
        raise HTTPException(422, "В экспорте отсутствуют id или name чата")
    chat = db.scalar(
        select(Chat).where(Chat.source == "telegram", Chat.external_id == chat_id)
    )
    if chat and chat.project_id != project.id:
        raise HTTPException(409, "Этот чат уже связан с другим проектом")
    if chat is None:
        if db.scalar(select(Chat.id).where(Chat.project_id == project.id)):
            raise HTTPException(409, "К проекту уже привязан другой чат")
        chat = Chat(
            id=new_id(),
            project_id=project.id,
            source="telegram",
            external_id=chat_id,
            name=data["name"][:200],
            type=str(data.get("type", "unknown"))[:50],
        )
        db.add(chat)
        db.flush()

    messages = {
        m.external_id: m
        for m in db.scalars(select(Message).where(Message.chat_id == chat.id))
    }
    participants = {
        p.source_author_id: p
        for p in db.scalars(select(Participant).where(Participant.chat_id == chat.id))
    }
    history = {}
    for revision in db.scalars(
        select(MessageRevision)
        .join(Message)
        .where(Message.chat_id == chat.id)
        .order_by(MessageRevision.revision_no)
    ):
        history.setdefault(revision.message_id, []).append(revision)
    local = {
        external_id(item.get("id")): external_id(
            source_metadata(item).get("external_id", item.get("id"))
        )
        for item in records
        if isinstance(item, dict)
    }
    seen = set()
    sent_times = []
    imported = updated = created = skipped = uncertain = media = services = 0
    for item in records:
        if not isinstance(item, dict) or not source_label(item.get("type")):
            skipped += 1
            continue
        source = source_metadata(item)
        mid = (
            local.get(external_id(item.get("id")))
            if external_id(item.get("id"))
            else None
        )
        sent_at = unix_time(item.get("date_unixtime"))
        if not mid or sent_at is None or mid in seen:
            skipped += 1
            continue
        seen.add(mid)
        sent_times.append(sent_at)
        service = item["type"] != "message" or source.get("sender_role") == "system"
        author = external_id(item.get("from_id")) if not service else None
        if author and author not in participants:
            participant = Participant(
                id=new_id(),
                chat_id=chat.id,
                source_author_id=author,
                display_name=str(item.get("from") or "")[:200] or None,
            )
            db.add(participant)
            participants[author] = participant
        body, formatting = normalized_text(item.get("text"))
        if not formatting and isinstance(item.get("text_entities"), list):
            formatting = [
                part for part in item["text_entities"] if isinstance(part, dict)
            ]
        attachment = attachment_metadata(item)
        raw_reply = external_id(item.get("reply_to_message_id"))
        reply = external_id(source.get("reply_to_external_id")) or local.get(
            raw_reply, raw_reply
        )
        snapshot = {
            "text": body,
            "text_format": formatting,
            "reply_to_external_id": reply,
            "participant_id": participants[author].id if author else None,
            "attachments": attachment,
            "source_type": "service" if service else "message",
            "service_action": source_label(item.get("action")) if service else None,
            "actor_id": external_id(item.get("actor_id")) if service else None,
            "actor_name": source_label(item.get("actor")) if service else None,
            "source_date": item.get("date"),
            "source_revision": source.get("revision")
            if type(source.get("revision")) is int
            else None,
            "source_payload": item,
        }
        message = messages.get(mid)
        if message is None:
            message = Message(
                id=new_id(),
                chat_id=chat.id,
                external_id=mid,
                sent_at=sent_at,
                observed_at=now,
            )
            db.add(message)
            messages[mid] = message
            imported += 1
        else:
            message.observed_at = now
        versions = history.setdefault(message.id, [])
        observation, changed, ambiguous = add_observation(
            db,
            message,
            versions,
            snapshot,
            unix_time(item.get("edited_unixtime")) or source_time(item.get("edited")),
            source_time(source.get("observed_at")),
            now,
        )
        created += int(observation and len(versions) > 1)
        updated += int(changed)
        uncertain += int(ambiguous)
        media += int(bool(attachment))
        services += int(service)

    missing = len(messages.keys() - seen)
    unresolved = sum(
        1
        for message in messages.values()
        if message.reply_to_external_id and message.reply_to_external_id not in messages
    )
    report = {
        "chat": {"id": chat.id, "name": chat.name, "external_id": chat.external_id},
        "total": len(records),
        "imported": imported,
        "updated": updated,
        "revisions_created": created,
        "skipped": skipped,
        "missing_from_export": missing,
        "attachments_present": media,
        "system_events": services,
        "unresolved_replies": unresolved,
        "uncertain_revisions": uncertain,
        "period_from": min(sent_times).isoformat() if sent_times else None,
        "period_to": max(sent_times).isoformat() if sent_times else None,
    }
    if key:
        if receipt is None:
            receipt = ImportReceipt(project_id=project.id, key=key)
            db.add(receipt)
        receipt.payload_hash, receipt.report, receipt.created_at = digest, report, now
    db.commit()
    return report, False
