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
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from actionflow.config import Settings
from actionflow.persistence.database import (
    Client,
    LoginSession,
    Project,
    User,
    database,
    utc_now,
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


def project_result(project: Project):
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
        "settings_version": None,
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
        allow_headers=["Content-Type"],
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
        code = {
            400: "malformed_request",
            401: "unauthenticated",
            404: "not_found",
            409: "conflict",
        }
        return error(
            exc.status_code, code.get(exc.status_code, "http_error"), exc.detail
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        if any(item["type"] == "json_invalid" for item in exc.errors()):
            return error(400, "malformed_request", "Некорректный JSON")
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
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Проект не найден")
        return project_result(project)

    router.include_router(projects)
    app.include_router(router)
    return app
