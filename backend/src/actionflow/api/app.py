from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select, text, tuple_
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from actionflow.api.project_settings import (
    SettingsInput,
    current_settings,
    participants_result,
    require_project,
    save_settings,
    settings_result,
    settings_versions,
)
from actionflow.api.telegram_import import (
    MAX_BYTES,
    as_utc,
    import_chat,
    revision_result,
)
from actionflow.config import Settings
from actionflow.persistence.database import (
    Client,
    LoginSession,
    Project,
    User,
    database,
    utc_now,
)
from actionflow.persistence.project_settings import (
    Chat,
    Message,
    MessageRevision,
    Participant,
    SettingsVersion,
)

COOKIE = "af_session"
SESSION_SECONDS = 12 * 60 * 60
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class Credentials(BaseModel):
    username: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ProjectInput(BaseModel):
    client_name: Name
    project_name: Name
    manager_name: Name


def project_result(project: Project, settings_version=None):
    created = project.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "id": project.id,
        "client": {"id": project.client.id, "name": project.client.name},
        "name": project.name,
        "manager_name": project.manager_name,
        "created_at": created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "message_count": 0,
        "chat": None,
        "settings_version": settings_version,
    }


def create_app(settings: Settings) -> FastAPI:
    engine, sessions = database(settings.database_url)
    passwords = PasswordHasher()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["Idempotent-Replayed"],
    )

    def error(status, code, message):
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": {},
                    "request_id": str(uuid4()),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc):
        if isinstance(exc.detail, dict):
            return error(exc.status_code, exc.detail["code"], exc.detail["message"])
        code = {
            400: "malformed_request",
            401: "unauthenticated",
            404: "not_found",
            409: "conflict",
            413: "payload_too_large",
            422: "validation_failed",
        }
        return error(
            exc.status_code, code.get(exc.status_code, "http_error"), exc.detail
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        if any(item["type"] == "json_invalid" for item in exc.errors()):
            return error(400, "malformed_request", "Некорректный JSON")
        for item in exc.errors():
            field = item["loc"][1:2]
            if field == ("timezone",):
                return error(
                    422,
                    "validation_failed",
                    "Укажите часовой пояс IANA, например Europe/Moscow",
                )
            if field == ("calendar",):
                return error(
                    422, "validation_failed", "Проверьте даты исключений календаря"
                )
            if field == ("work_hours",):
                return error(
                    422,
                    "validation_failed",
                    "Проверьте рабочие дни и время начала и окончания",
                )
        return error(422, "validation_failed", "Проверьте заполнение полей")

    @app.exception_handler(OperationalError)
    async def database_error(_request, _exc):
        return error(503, "dependency_unavailable", "База данных недоступна")

    @app.exception_handler(Exception)
    async def internal_error(_request, _exc):
        return error(500, "internal_error", "Не удалось выполнить запрос")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    def transaction():
        with sessions() as db:
            yield db

    Db = Depends(transaction)

    def check_origin(request: Request):
        origin = request.headers.get("origin")
        if origin is None:
            try:
                referer = urlsplit(request.headers.get("referer", ""))
                origin = f"{referer.scheme}://{referer.netloc}"
            except ValueError:
                raise HTTPException(400, "Недопустимый источник запроса") from None
        if origin != settings.frontend_origin:
            raise HTTPException(400, "Недопустимый источник запроса")

    def authenticated(request: Request, db=Db):
        token = request.cookies.get(COOKIE, "")
        if len(token) == 43:
            login = db.scalar(
                select(LoginSession).where(
                    LoginSession.token_hash == sha256(token.encode()).hexdigest(),
                    LoginSession.expires_at > utc_now(),
                )
            )
            if login is not None:
                return login
        raise HTTPException(401, "Войдите в приложение")

    SessionRequired = Depends(authenticated)
    router = APIRouter(prefix="/api/v1")
    projects = APIRouter(dependencies=[SessionRequired])

    @router.post("/auth/login", dependencies=[Depends(check_origin)])
    def login(credentials: Credentials, response: Response, db=Db):
        user = db.get(User, 1)
        if user is None:
            raise HTTPException(401, "Неверное имя пользователя или пароль")
        try:
            passwords.verify(user.password_hash, credentials.password)
        except VerifyMismatchError:
            raise HTTPException(401, "Неверное имя пользователя или пароль") from None
        if credentials.username != user.login:
            raise HTTPException(401, "Неверное имя пользователя или пароль")
        token = token_urlsafe(32)
        now = utc_now()
        db.add(
            LoginSession(
                user_id=user.id,
                token_hash=sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(seconds=SESSION_SECONDS),
                last_used_at=now,
            )
        )
        db.commit()
        response.set_cookie(
            COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict"
        )
        return {"username": user.login}

    @router.get("/auth/me")
    def me(session=SessionRequired):
        return {"username": session.user.login}

    @router.post(
        "/auth/logout",
        status_code=204,
        dependencies=[SessionRequired, Depends(check_origin)],
    )
    def logout(response: Response, session=SessionRequired, db=Db):
        db.delete(session)
        db.commit()
        response.delete_cookie(COOKIE, httponly=True, samesite="strict")

    @projects.get("/projects")
    def list_projects(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = None, db=Db
    ):
        query = select(Project).order_by(Project.id).limit(limit + 1)
        if cursor:
            query = query.where(Project.id > cursor)
        rows = list(db.scalars(query))
        return {
            "items": [project_result(project) for project in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
        }

    @projects.post("/projects", status_code=201, dependencies=[Depends(check_origin)])
    def add_project(data: ProjectInput, db=Db):
        try:
            client = db.scalar(select(Client).where(Client.name == data.client_name))
            if client is None:
                try:
                    with db.begin_nested():
                        client = Client(name=data.client_name)
                        db.add(client)
                        db.flush()
                except IntegrityError:
                    client = db.scalar(
                        select(Client).where(Client.name == data.client_name)
                    )
                    if client is None:
                        raise
            project = Project(
                client=client, name=data.project_name, manager_name=data.manager_name
            )
            db.add(project)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Такой проект уже есть у клиента") from None
        return project_result(project)

    @projects.get("/projects/{project_id}")
    def get_project(project_id: str, db=Db):
        project = require_project(db, project_id)
        version = current_settings(db, project_id)
        return project_result(project, settings_result(version) if version else None)

    async def read_export(request: Request):
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Максимальный размер экспорта — 50 МБ")
            payload.extend(chunk)
        return payload

    ExportBody = Depends(read_export)

    @projects.post(
        "/projects/{project_id}/messages/import", dependencies=[Depends(check_origin)]
    )
    def upload_chat(
        project_id: str, request: Request, response: Response, payload=ExportBody, db=Db
    ):
        project = require_project(db, project_id, lock=True)
        key = request.headers.get("Idempotency-Key")
        if key is not None:
            key = key.strip()
            if not 0 < len(key) <= 200:
                raise HTTPException(400, "Некорректный ключ повтора")
        try:
            report, replayed = import_chat(db, project, payload, key)
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                409, "Чат уже привязан или импорт изменился. Повторите запрос."
            ) from None
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return report

    @projects.get("/projects/{project_id}/messages")
    def list_messages(
        project_id: str,
        limit: int = Query(100, ge=1, le=500),
        cursor: str | None = None,
        db=Db,
    ):
        require_project(db, project_id)
        chat = db.scalar(select(Chat).where(Chat.project_id == project_id))
        if chat is None:
            return {"chat": None, "items": [], "next_cursor": None}
        query = (
            select(Message)
            .where(Message.chat_id == chat.id)
            .order_by(Message.sent_at, Message.external_id)
        )
        if cursor:
            previous = db.scalar(
                select(Message).where(
                    Message.chat_id == chat.id, Message.external_id == cursor
                )
            )
            if previous is None:
                raise HTTPException(400, "Некорректный курсор переписки")
            query = query.where(
                tuple_(Message.sent_at, Message.external_id)
                > (previous.sent_at, previous.external_id)
            )
        rows = list(db.scalars(query.limit(limit + 1)))
        items = rows[:limit]
        participants = {
            person.id: person
            for person in db.scalars(
                select(Participant).where(Participant.chat_id == chat.id)
            )
        }
        settings_version = current_settings(db, project_id)
        sides = (
            {side.source_author_id: side.side for side in settings_version.sides}
            if settings_version
            else {}
        )
        active = {
            revision.message_id: revision
            for revision in db.scalars(
                select(MessageRevision).where(
                    MessageRevision.message_id.in_([item.id for item in items]),
                    MessageRevision.is_current.is_(True),
                )
            )
        }
        output = []
        for item in items:
            revision = active[item.id]
            details = revision_result(revision)
            details["revision_id"] = details.pop("id")
            author = participants.get(item.participant_id)
            output.append(
                {
                    **details,
                    "id": item.id,
                    "external_id": item.external_id,
                    "sent_at": as_utc(item.sent_at),
                    "last_observed_at": as_utc(item.observed_at),
                    "author": {
                        "id": author.source_author_id if author else None,
                        "name": author.display_name if author else None,
                        "side": None
                        if details["source_type"] == "service"
                        else sides.get(author.source_author_id, "unknown")
                        if author
                        else "unknown",
                    },
                }
            )
        return {
            "chat": {"id": chat.id, "name": chat.name},
            "items": output,
            "next_cursor": items[-1].external_id if len(rows) > limit else None,
        }

    @projects.get("/projects/{project_id}/messages/{message_id}/revisions")
    def list_revisions(project_id: str, message_id: str, db=Db):
        require_project(db, project_id)
        message = db.scalar(
            select(Message)
            .join(Chat)
            .where(Chat.project_id == project_id, Message.id == message_id)
        )
        if message is None:
            raise HTTPException(404, "Сообщение не найдено")
        revisions = db.scalars(
            select(MessageRevision)
            .where(MessageRevision.message_id == message.id)
            .order_by(MessageRevision.revision_no)
        )
        return {"items": [revision_result(revision) for revision in revisions]}

    @projects.get("/projects/{project_id}/participants")
    def get_participants(project_id: str, db=Db):
        require_project(db, project_id)
        return participants_result(db, project_id, current_settings(db, project_id))

    @projects.get("/projects/{project_id}/settings")
    def get_settings(project_id: str, db=Db):
        require_project(db, project_id)
        version = current_settings(db, project_id)
        if version is None:
            raise HTTPException(404, "Настройки проекта ещё не сохранены")
        return settings_result(version)

    @projects.post(
        "/projects/{project_id}/settings",
        status_code=201,
        dependencies=[Depends(check_origin)],
    )
    def post_settings(project_id: str, data: SettingsInput, db=Db):
        project = require_project(db, project_id, lock=True)
        return save_settings(db, project, data)

    @projects.get("/projects/{project_id}/settings/versions")
    def get_versions(
        project_id: str,
        limit: int = Query(50, ge=1, le=100),
        cursor: int | None = Query(None, ge=1),
        db=Db,
    ):
        require_project(db, project_id)
        query = settings_versions(project_id).limit(limit + 1)
        if cursor is not None:
            query = query.where(SettingsVersion.version_no < cursor)
        versions = list(db.scalars(query))
        return {
            "items": [settings_result(version) for version in versions[:limit]],
            "next_cursor": str(versions[limit - 1].version_no)
            if len(versions) > limit
            else None,
        }

    @projects.get("/projects/{project_id}/settings/versions/{version_id}")
    def get_version(project_id: str, version_id: str, db=Db):
        require_project(db, project_id)
        version = db.scalar(
            settings_versions(project_id).where(SettingsVersion.id == version_id)
        )
        if version is None:
            raise HTTPException(404, "Версия настроек не найдена")
        return settings_result(version)

    router.include_router(projects)
    app.include_router(router)
    return app
