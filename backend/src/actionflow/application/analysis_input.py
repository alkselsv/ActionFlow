import json
from hashlib import sha256

from sqlalchemy import func, select

from actionflow.persistence.analysis import AnalysisInputMessage, AnalysisRun
from actionflow.persistence.database import as_utc, utc_now
from actionflow.persistence.project_settings import (
    Message,
    MessageRevision,
    Participant,
    SettingsVersion,
    SourceObservation,
)

MAX_MESSAGES = 50_000
TERMINAL = {"succeeded", "failed"}


class InputTooLarge(Exception):
    pass


def sent_before_end(run):
    return (
        Message.chat_id == run.chat_id,
        Message.sent_at < run.period_end,
    )


def input_rows(run):
    source_observed = (
        select(func.min(SourceObservation.source_observed_at))
        .where(
            SourceObservation.revision_id == MessageRevision.id,
            SourceObservation.recorded_at <= run.created_at,
        )
        .correlate(MessageRevision)
        .scalar_subquery()
    )
    available = func.coalesce(source_observed, MessageRevision.observed_at)
    choices = (
        select(
            Message.id.label("message_id"),
            Message.sent_at,
            MessageRevision.id.label("revision_id"),
            available.label("available_at"),
            source_observed.label("source_available_at"),
            func.row_number()
            .over(
                partition_by=Message.id,
                order_by=(
                    func.coalesce(
                        MessageRevision.snapshot["source_revision"].as_integer(), 0
                    ).desc(),
                    func.coalesce(MessageRevision.edited_at, Message.sent_at).desc(),
                    func.coalesce(
                        MessageRevision.snapshot["source_update_id"].as_integer(), 0
                    ).desc(),
                    MessageRevision.revision_no.desc(),
                ),
            )
            .label("position"),
        )
        .join(MessageRevision, MessageRevision.message_id == Message.id)
        .where(
            *sent_before_end(run),
            MessageRevision.observed_at <= run.created_at,
            available <= run.as_of,
            (
                MessageRevision.edited_at.is_(None)
                | (MessageRevision.edited_at <= run.as_of)
            ),
            MessageRevision.ordering_uncertain.is_(False),
        )
        .subquery()
    )
    return select(choices).where(choices.c.position == 1).order_by(choices.c.message_id)


def check_limit(db, run):
    if run.chat_id is not None:
        count = db.scalar(
            select(func.count()).select_from(
                input_rows(run).limit(MAX_MESSAGES + 1).subquery()
            )
        )
        if count > MAX_MESSAGES:
            raise InputTooLarge


def prepare_input(sessions, run_id, deadline=None, *, finalize=True):
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL:
            return
        if run.input_hash is not None:
            if finalize:
                run.status = "succeeded"
                run.stage = "input_ready"
                run.finished_at = run.finished_at or utc_now()
            return
        run.status = "running"
        run.stage = "prepare"
        run.started_at = run.started_at or utc_now()

    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run.status in TERMINAL:
            return
        if run.input_hash is not None:
            if finalize:
                run.status = "succeeded"
                run.stage = "input_ready"
                run.finished_at = run.finished_at or utc_now()
            return
        check_limit(db, run)
        settings = (
            db.get(SettingsVersion, run.settings_version_id)
            if run.settings_version_id
            else None
        )
        sides = (
            {side.source_author_id: side.side for side in settings.sides}
            if settings
            else {}
        )
        digest = sha256()
        digest.update(json.dumps([run.settings_version_id]).encode())
        counters = {
            "attachments": 0,
            "unknown_authors": 0,
            "system_events": 0,
            "message_count": 0,
            "context_messages": 0,
        }
        selected = input_rows(run).subquery()
        records = (
            db.execute(
                select(
                    selected.c.message_id,
                    selected.c.sent_at,
                    selected.c.available_at,
                    selected.c.source_available_at,
                    MessageRevision,
                    Participant.source_author_id,
                )
                .join(MessageRevision, MessageRevision.id == selected.c.revision_id)
                .outerjoin(
                    Participant,
                    Participant.id
                    == MessageRevision.snapshot["participant_id"].as_string(),
                )
                .order_by(selected.c.message_id)
                .limit(MAX_MESSAGES + 1)
            )
            if run.chat_id
            else []
        )
        for (
            message_id,
            sent_at,
            available,
            source_available,
            revision,
            author_id,
        ) in records:
            source = revision.snapshot
            side = (
                "system"
                if source.get("source_type") == "service"
                else sides.get(author_id, "unknown")
            )
            counters["attachments"] += bool(source.get("attachments"))
            counters["system_events"] += side == "system"
            counters["unknown_authors"] += side == "unknown"
            role = (
                "window" if as_utc(sent_at) >= as_utc(run.period_start) else "context"
            )
            counters["context_messages"] += role == "context"
            counters["message_count"] += 1
            if counters["message_count"] > MAX_MESSAGES:
                raise InputTooLarge
            db.add(
                AnalysisInputMessage(
                    run_id=run.id,
                    message_id=message_id,
                    revision_id=revision.id,
                    sent_at=sent_at,
                    observed_at=available,
                    role=role,
                    participant_side=side,
                    source_observed_at=source_available,
                )
            )
            digest.update(
                json.dumps(
                    [
                        message_id,
                        revision.id,
                        as_utc(sent_at).isoformat(),
                        as_utc(available).isoformat(),
                        role,
                    ],
                    separators=(",", ":"),
                ).encode()
            )
        run.input_hash = digest.hexdigest()
        total = (
            db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    *sent_before_end(run),
                    select(MessageRevision.id)
                    .where(
                        MessageRevision.message_id == Message.id,
                        MessageRevision.observed_at <= run.created_at,
                    )
                    .exists(),
                )
            )
            if run.chat_id
            else 0
        )
        uncertain = (
            db.scalar(
                select(func.count())
                .select_from(MessageRevision)
                .join(Message, Message.id == MessageRevision.message_id)
                .where(
                    *sent_before_end(run),
                    MessageRevision.observed_at <= min(run.created_at, run.as_of),
                    MessageRevision.ordering_uncertain.is_(True),
                )
            )
            if run.chat_id
            else 0
        )
        run.limitations = {
            **counters,
            "missing_settings": settings is None,
            "unavailable_messages": max(0, total - counters["message_count"]),
            "history_incomplete": True,
            "uncertain_revisions": uncertain,
        }
        if deadline is not None and utc_now() >= deadline:
            raise TimeoutError("Activity deadline elapsed before saving the input")
        run.stage = "input_ready"
        if finalize:
            run.status, run.finished_at = "succeeded", utc_now()
        else:
            run.status, run.finished_at = "running", None


def fail_run(sessions, run_id, stage, error_code, retryable):
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is not None and run.status not in TERMINAL:
            run.status, run.stage = "failed", stage
            run.error_code, run.retryable = error_code, retryable
            run.finished_at = utc_now()
