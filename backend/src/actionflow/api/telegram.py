from datetime import UTC, datetime
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from actionflow.api.project_settings import require_project
from actionflow.config import Settings
from actionflow.persistence.database import new_id, utc_now
from actionflow.persistence.project_settings import Chat
from actionflow.persistence.telegram import TelegramConnection

ChatId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=20,
        pattern=r"^-?[0-9]+$",
    ),
]
ChatName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ConnectInput(BaseModel):
    chat_id: ChatId
    name: ChatName | None = None


def _iso(value: datetime | None):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def connection_result(connection: TelegramConnection, chat: Chat):
    return {
        "chat_id": chat.external_id,
        "name": chat.name,
        "type": chat.type,
        "status": connection.status,
        "connected_at": _iso(connection.connected_at),
        "last_event_at": _iso(connection.last_event_at),
        "last_error": connection.last_error,
    }


def register_telegram_routes(projects, Db, origin, settings: Settings):
    @projects.get("/projects/{project_id}/telegram")
    def get_telegram(project_id: str, db=Db):
        require_project(db, project_id)
        chat = db.scalar(select(Chat).where(Chat.project_id == project_id))
        connection = db.get(TelegramConnection, chat.id) if chat else None
        return {
            "connection": connection_result(connection, chat)
            if connection and chat
            else None,
            "bot_configured": bool(settings.telegram_bot_token),
        }

    @projects.post(
        "/projects/{project_id}/telegram/connect",
        dependencies=[origin],
    )
    def connect_telegram(project_id: str, data: ConnectInput, db=Db):
        project = require_project(db, project_id, lock=True)
        chat = db.scalar(
            select(Chat).where(
                Chat.source == "telegram", Chat.external_id == data.chat_id
            )
        )
        if chat is not None and chat.project_id != project.id:
            raise HTTPException(409, "Этот чат уже связан с другим проектом")
        existing = db.scalar(select(Chat).where(Chat.project_id == project.id))
        if existing is not None and existing.external_id != data.chat_id:
            raise HTTPException(409, "К проекту уже привязан другой чат")
        if existing is not None and existing.source != "telegram":
            raise HTTPException(409, "К проекту уже привязан другой источник")

        now = utc_now()
        try:
            if chat is None:
                chat = Chat(
                    id=new_id(),
                    project_id=project.id,
                    source="telegram",
                    external_id=data.chat_id,
                    name=data.name or "Telegram-чат",
                    type="group",
                )
                db.add(chat)
                db.flush()
            elif data.name:
                chat.name = data.name
            connection = db.get(TelegramConnection, chat.id)
            if connection is None:
                connection = TelegramConnection(
                    chat_id=chat.id,
                    status="active",
                    connected_at=now,
                    updated_at=now,
                )
                db.add(connection)
            else:
                connection.status = "active"
                connection.updated_at = now
                connection.last_error = None
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Чат уже связан с другим проектом") from None
        return {
            "connection": connection_result(connection, chat),
            "bot_configured": bool(settings.telegram_bot_token),
        }

    @projects.post(
        "/projects/{project_id}/telegram/disconnect",
        dependencies=[origin],
    )
    def disconnect_telegram(project_id: str, db=Db):
        require_project(db, project_id)
        chat = db.scalar(select(Chat).where(Chat.project_id == project_id))
        connection = db.get(TelegramConnection, chat.id) if chat else None
        if connection is None:
            raise HTTPException(404, "Telegram-чат не подключён")
        connection.status = "disconnected"
        connection.updated_at = utc_now()
        connection.last_error = None
        db.commit()
        return {
            "connection": connection_result(connection, chat),
            "bot_configured": bool(settings.telegram_bot_token),
        }
