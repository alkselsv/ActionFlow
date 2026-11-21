import json
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from actionflow.persistence.database import Project, utc_now
from actionflow.persistence.project_settings import Chat, Message, Participant


def import_chat(db: Session, project: Project, payload: bytes):
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Файл не является корректным UTF-8 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise HTTPException(422, "Ожидается экспорт Telegram с массивом messages")
    external_chat_id = str(data.get("id", "")).strip()
    if not external_chat_id or not isinstance(data.get("name"), str):
        raise HTTPException(422, "В экспорте отсутствуют id или name чата")
    chat = db.scalar(select(Chat).where(Chat.source == "telegram", Chat.external_id == external_chat_id))
    if chat and chat.project_id != project.id:
        raise HTTPException(409, "Этот чат уже связан с другим проектом")
    if chat:
        raise HTTPException(409, "Чат уже импортирован; повторный импорт поддержит задача #11")
    chat = Chat(project_id=project.id, source="telegram", external_id=external_chat_id,
                name=data["name"][:200], type=str(data.get("type", "unknown"))[:50])
    db.add(chat)
    db.flush()
    participants = {}
    parsed, skipped = [], 0
    now = utc_now()
    records = data["messages"]
    for item in records:
        if not isinstance(item, dict) or item.get("type") != "message":
            skipped += 1
            continue
        mid, author_id = item.get("id"), item.get("from_id")
        stamp = item.get("date_unixtime")
        if mid is None or not author_id or stamp is None:
            skipped += 1
            continue
        try:
            sent_at = datetime.fromtimestamp(int(stamp), UTC)
        except (ValueError, TypeError, OverflowError):
            skipped += 1
            continue
        parts = item.get("text", "")
        if isinstance(parts, str):
            plain, formatting = parts, []
        elif isinstance(parts, list):
            plain = "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in parts if isinstance(p, (str, dict)))
            formatting = [p for p in parts if isinstance(p, dict)]
        else:
            plain, formatting = "", []
        author_id = str(author_id)
        if author_id not in participants:
            participant = db.scalar(select(Participant).where(Participant.chat_id == chat.id, Participant.source_author_id == author_id))
            if participant is None:
                participant = Participant(chat_id=chat.id, source_author_id=author_id, display_name=str(item.get("from", ""))[:200] or None)
                db.add(participant)
                db.flush()
            participants[author_id] = participant
        source = item.get("_actionflow_synthetic_source", {})
        external_id = str(source.get("external_id", mid))
        parsed.append(Message(chat_id=chat.id, external_id=external_id, participant_id=participants[author_id].id,
                              sent_at=sent_at, observed_at=now, text=plain, text_format=formatting,
                              reply_to_external_id=str(source.get("reply_to_external_id", item.get("reply_to_message_id"))) if source.get("reply_to_external_id", item.get("reply_to_message_id")) is not None else None))
    parsed.sort(key=lambda message: (message.sent_at, message.external_id))
    db.add_all(parsed)
    db.commit()
    return {"chat": {"id": chat.id, "name": chat.name, "external_id": chat.external_id},
            "imported": len(parsed), "skipped": skipped, "total": len(records)}
