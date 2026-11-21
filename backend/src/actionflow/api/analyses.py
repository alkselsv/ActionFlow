from datetime import timedelta
from hashlib import sha256

from fastapi import HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, model_validator
from sqlalchemy import select

from actionflow.api.project_settings import require_project
from actionflow.api.telegram_import import revision_result
from actionflow.application.analysis_input import InputTooLarge, check_limit
from actionflow.persistence.analysis import (
    AnalysisInputMessage,
    AnalysisRun,
    OutboxEvent,
)
from actionflow.persistence.database import as_utc, new_id, utc_now
from actionflow.persistence.project_settings import (
    Chat,
    MessageRevision,
    SettingsVersion,
)


class AnalysisRequest(BaseModel):
    period_start: AwareDatetime
    period_end: AwareDatetime
    as_of: AwareDatetime
    apply_to_queue: bool = True
    settings_version_id: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        self.period_start, self.period_end, self.as_of = (
            as_utc(value) for value in (self.period_start, self.period_end, self.as_of)
        )
        if not self.period_start < self.period_end <= self.as_of:
            raise ValueError(
                "Начало периода должно быть раньше конца, а конец — не позже момента оценки"
            )
        return self


def start_result(run):
    return {"run_id": run.id, "status": "pending", "workflow_id": run.workflow_id}


def run_result(run):
    return {
        **start_result(run),
        "status": run.status,
        "stage": run.stage,
        "project_id": run.project_id,
        "period_start": as_utc(run.period_start),
        "period_end": as_utc(run.period_end),
        "as_of": as_utc(run.as_of),
        "settings_version_id": run.settings_version_id,
        "input_hash": run.input_hash,
        "limitations": run.limitations,
        "error_code": run.error_code,
        "temporal_run_id": run.temporal_run_id,
        "cost_usd": None,
        "duration_seconds": (
            as_utc(run.finished_at) - as_utc(run.started_at)
        ).total_seconds()
        if run.finished_at and run.started_at
        else None,
        "retryable": run.retryable,
        "created_at": as_utc(run.created_at),
        "started_at": as_utc(run.started_at),
        "finished_at": as_utc(run.finished_at),
    }


def require_run(db, run_id):
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "Запуск не найден")
    return run


def register_analysis_routes(projects, router, Db, session_required, origin_required):
    @projects.post(
        "/projects/{project_id}/analyses",
        status_code=202,
        dependencies=[origin_required],
    )
    def request_analysis(
        project_id: str,
        data: AnalysisRequest,
        request: Request,
        response: Response,
        db=Db,
    ):
        require_project(db, project_id, lock=True)
        key = request.headers.get("Idempotency-Key", "").strip()
        if not 0 < len(key) <= 200:
            raise HTTPException(400, "Для запуска укажите Idempotency-Key")
        digest = sha256(data.model_dump_json().encode()).hexdigest()
        previous = db.scalar(
            select(AnalysisRun).where(
                AnalysisRun.project_id == project_id,
                AnalysisRun.idempotency_key == key,
            )
        )
        created = utc_now()
        if previous and as_utc(previous.created_at) + timedelta(hours=24) > created:
            if digest != previous.request_hash:
                raise HTTPException(
                    409,
                    {
                        "code": "idempotency_conflict",
                        "message": "Ключ уже использован для другого запуска",
                    },
                )
            response.headers["Idempotent-Replayed"] = "true"
            return start_result(previous)
        if previous:
            previous.idempotency_key = None
            db.flush()
        query = (
            select(SettingsVersion.id)
            .where(
                SettingsVersion.project_id == project_id,
                SettingsVersion.created_at <= min(data.as_of, created),
            )
            .order_by(SettingsVersion.version_no.desc())
            .limit(1)
        )
        if data.settings_version_id:
            query = query.where(SettingsVersion.id == data.settings_version_id)
        version_id = db.scalar(query)
        if data.settings_version_id and version_id is None:
            raise HTTPException(
                422, "Версия настроек недоступна для проекта на момент оценки"
            )
        run_id = new_id()
        run = AnalysisRun(
            id=run_id,
            project_id=project_id,
            created_at=created,
            chat_id=db.scalar(select(Chat.id).where(Chat.project_id == project_id)),
            settings_version_id=version_id,
            idempotency_key=key,
            request_hash=digest,
            workflow_id=f"analysis:{run_id}",
            **data.model_dump(exclude={"settings_version_id"}),
        )
        try:
            check_limit(db, run)
        except InputTooLarge:
            raise HTTPException(
                422,
                {
                    "code": "analysis_input_too_large",
                    "message": "В срезе больше 50 000 сообщений. Сократите период.",
                },
            ) from None
        db.add(run)
        db.flush()
        db.add(OutboxEvent(aggregate_id=run_id, event_key=run_id))
        db.commit()
        return start_result(run)

    @projects.get("/projects/{project_id}/analyses")
    def project_analyses(
        project_id: str,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
        db=Db,
    ):
        require_project(db, project_id)
        query = (
            select(AnalysisRun)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        )
        if cursor:
            anchor = require_run(db, cursor)
            if anchor.project_id != project_id:
                raise HTTPException(404, "Запуск не найден")
            query = query.where(
                (AnalysisRun.created_at < anchor.created_at)
                | (
                    (AnalysisRun.created_at == anchor.created_at)
                    & (AnalysisRun.id < anchor.id)
                )
            )
        rows = list(db.scalars(query.limit(limit + 1)))
        return {
            "items": [run_result(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
        }

    @router.get("/analyses/{run_id}", dependencies=[session_required])
    def analysis_status(run_id: str, db=Db):
        return run_result(require_run(db, run_id))

    @router.get("/analyses/{run_id}/input", dependencies=[session_required])
    def analysis_input(
        run_id: str,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
        db=Db,
    ):
        run = require_run(db, run_id)
        query = (
            select(AnalysisInputMessage, MessageRevision)
            .join(
                MessageRevision, MessageRevision.id == AnalysisInputMessage.revision_id
            )
            .where(AnalysisInputMessage.run_id == run.id)
            .order_by(AnalysisInputMessage.message_id)
        )
        if cursor:
            query = query.where(AnalysisInputMessage.message_id > cursor)
        rows = list(db.execute(query.limit(limit + 1)))
        return {
            "run": run_result(run),
            "items": [
                {
                    "message_id": item.message_id,
                    "revision_id": item.revision_id,
                    "role": item.role,
                    "participant_side": item.participant_side,
                    "sent_at": as_utc(item.sent_at),
                    "available_at": as_utc(item.observed_at),
                    "revision": {
                        **revision_result(revision),
                        "source_observed_at": as_utc(item.source_observed_at),
                    },
                }
                for item, revision in rows[:limit]
            ],
            "next_cursor": rows[limit - 1][0].message_id if len(rows) > limit else None,
        }
