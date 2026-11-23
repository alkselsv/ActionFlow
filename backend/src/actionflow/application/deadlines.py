"""Advance due cards without rewriting the historical analysis observation."""

from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import and_, func, or_, select

from actionflow.application.analysis_input import input_rows
from actionflow.detectors.contracts import InputMessage
from actionflow.detectors.local_baseline import _is_request
from actionflow.domain.priority import PrioritySignals, calculate_priority
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import Project, as_utc
from actionflow.persistence.findings import (
    Commitment,
    DeadlineObservation,
    Finding,
    FindingObservation,
)
from actionflow.persistence.project_settings import (
    Chat,
    Message,
    MessageRevision,
    Participant,
    ParticipantSide,
)
from actionflow.persistence.telegram import TelegramConnection, TelegramGap
from actionflow.telegram_ingest import IngestReport, _request_analysis


def _active_chats():
    return (
        select(Chat)
        .join(TelegramConnection, TelegramConnection.chat_id == Chat.id)
        .where(TelegramConnection.status == "active")
    )


def reconcile(sessions, at: datetime, slot: int):
    with sessions.begin() as db:
        chats = list(db.scalars(_active_chats().order_by(Chat.project_id)))
        report = IngestReport()
        for chat in chats:
            _request_analysis(db, chat, at, 0, report, scheduled_slot=slot)
        return report.analysis_runs_created


def advance_deadlines(sessions, now: datetime) -> str | None:
    with sessions.begin() as db:
        active_projects = _active_chats().with_only_columns(Chat.project_id)
        unanswered = and_(
            Finding.finding_type == "unanswered",
            Finding.sla_state == "pending",
            Finding.first_response_at.is_(None),
            Finding.sla_due_at.is_not(None),
        )
        commitments = and_(
            Finding.finding_type == "commitment",
            Finding.commitment_status == "open",
            Finding.commitment_due_at.is_not(None),
        )
        base = select(Finding).where(
            Finding.project_id.in_(active_projects),
            Finding.status.in_(("new", "confirmed", "snoozed")),
            or_(unanswered, commitments),
        )
        due = list(
            db.execute(
                base.with_only_columns(Finding.id, Finding.project_id)
                .where(
                    or_(
                        and_(unanswered, Finding.sla_due_at <= now),
                        and_(commitments, Finding.commitment_due_at < now),
                    )
                )
                .order_by(Finding.project_id, Finding.id)
            )
        )
        for finding_id, project_id in due:
            db.scalar(select(Project).where(Project.id == project_id).with_for_update())
            # A committed message may already be waiting for its analysis.
            pending = db.scalar(
                select(AnalysisRun.id)
                .where(
                    AnalysisRun.project_id == project_id,
                    AnalysisRun.apply_to_queue.is_(True),
                    AnalysisRun.status.in_(("pending", "queued", "running")),
                )
                .limit(1)
            )
            if pending:
                continue
            finding = db.scalar(base.where(Finding.id == finding_id).with_for_update())
            if finding is not None:
                _advance(db, finding, now)
        db.flush()
        future = db.scalar(
            select(
                func.min(func.coalesce(Finding.sla_due_at, Finding.commitment_due_at))
            ).where(
                Finding.id.in_(base.with_only_columns(Finding.id)),
                or_(Finding.sla_due_at > now, Finding.commitment_due_at >= now),
            )
        )
        return as_utc(future).isoformat() if future else None


def _advance(db, finding, now):
    current = (
        db.get(AnalysisRun, finding.current_run_id) if finding.current_run_id else None
    )
    if current is None:
        return
    reason = finding.sla_reason or {}
    limits = tuple(reason.get("limitations", ()))
    is_sla = finding.finding_type == "unanswered"
    due_at = as_utc(finding.sla_due_at if is_sla else finding.commitment_due_at)
    if due_at is None or (now < due_at if is_sla else now <= due_at):
        return
    observation = db.scalar(
        select(FindingObservation).where(
            FindingObservation.finding_id == finding.id,
            FindingObservation.run_id == current.id,
        )
    )
    settings_id = (
        observation.settings_version_id if observation else current.settings_version_id
    )
    response = None
    state = finding.sla_state
    if is_sla:
        response, incomplete = _first_reaction(
            db, finding, current.chat_id, current.settings_version_id, now, due_at
        )
        if incomplete and (response is None or response > due_at):
            state = "uncertain"
            limits = (*limits, "telegram_coverage_gap")
        else:
            state = "met" if response and response <= due_at else "breached"
    commitment = db.get(Commitment, finding.subject_id) if not is_sla else None
    if commitment and commitment.due_settings_version_id:
        settings_id = commitment.due_settings_version_id
    anchor = db.get(Message, finding.anchor_message_id)
    commitment_uncertain = bool(commitment and commitment.due_uncertain) or bool(
        commitment and anchor and _has_gap(db, current.chat_id, anchor.sent_at, now)
    )
    previous_priority = finding.priority_reason or {}
    priority = calculate_priority(
        PrioritySignals(
            finding_type=finding.finding_type,
            as_of=now,
            status=finding.status,
            sla_state=state,
            commitment_due_at=None if is_sla else due_at,
            commitment_status=finding.commitment_status,
            commitment_due_uncertain=commitment_uncertain,
            explicit_risk="explicit_contract_or_operational_risk"
            in previous_priority.get("reasons", ()),
            limitations=limits,
        )
    )
    if not is_sla and previous_priority.get("evaluated_deadline") == due_at.isoformat():
        return
    if is_sla:
        finding.sla_state = state
        finding.first_response_at = response
        finding.sla_reason = {
            **reason,
            "evaluated_at": now.isoformat(),
            "trigger": "deadline_timer",
            "settings_version_id": settings_id,
            "limitations": list(limits),
            "first_response_at": response.isoformat() if response else None,
            "uncertain": state == "uncertain",
        }
    finding.priority = priority.priority
    finding.priority_reason = {
        **priority.reason,
        "evaluated_deadline": due_at.isoformat(),
        "evaluated_at": now.isoformat(),
        "settings_version_id": settings_id,
        "trigger": "deadline_timer",
    }
    finding.version += 1
    finding.updated_at = now
    outcome = state if is_sla else ("uncertain" if commitment_uncertain else "overdue")
    audit_id = sha256(
        f"{finding.id}|{due_at.isoformat()}|{settings_id}|{outcome}".encode()
    ).hexdigest()
    if db.get(DeadlineObservation, audit_id) is None:
        db.add(
            DeadlineObservation(
                id=audit_id,
                finding_id=finding.id,
                due_at=due_at,
                evaluated_at=now,
                state=outcome,
                settings_version_id=settings_id,
            )
        )


def _has_gap(db, chat_id, start, end):
    return (
        db.scalar(
            select(TelegramGap.id)
            .where(
                TelegramGap.chat_id == chat_id,
                TelegramGap.start_at <= end,
                or_(TelegramGap.end_at.is_(None), TelegramGap.end_at > start),
            )
            .limit(1)
        )
        is not None
    )


def _first_reaction(db, finding, chat_id, settings_id, now, due_at):
    anchor = db.get(Message, finding.anchor_message_id)
    if anchor is None or anchor.chat_id != chat_id:
        return None, True
    snapshot_rows = input_rows(
        AnalysisRun(
            chat_id=chat_id,
            period_end=now + timedelta(microseconds=1),
            as_of=now,
            created_at=now,
        )
    ).subquery()
    rows = list(
        db.execute(
            select(Message, MessageRevision, ParticipantSide.side)
            .join(snapshot_rows, snapshot_rows.c.message_id == Message.id)
            .join(MessageRevision, MessageRevision.id == snapshot_rows.c.revision_id)
            .outerjoin(
                Participant,
                Participant.id
                == MessageRevision.snapshot["participant_id"].as_string(),
            )
            .outerjoin(
                ParticipantSide,
                and_(
                    ParticipantSide.settings_version_id == settings_id,
                    ParticipantSide.source_author_id == Participant.source_author_id,
                ),
            )
            .where(
                Message.chat_id == chat_id,
                Message.id != anchor.id,
                or_(
                    Message.sent_at > anchor.sent_at,
                    and_(
                        Message.sent_at == anchor.sent_at,
                        Message.external_id > anchor.external_id,
                    ),
                ),
            )
            .order_by(Message.sent_at, Message.external_id)
            .limit(101)
        )
    )
    incomplete = (
        _has_gap(db, chat_id, anchor.sent_at, min(due_at, now)) or len(rows) > 100
    )
    for message, revision, side in rows[:100]:
        snapshot = revision.snapshot
        item = InputMessage(
            message.id,
            revision.id,
            message.external_id,
            snapshot.get("text", ""),
            as_utc(message.sent_at),
            "window",
            side or "unknown",
            snapshot.get("source_type", "message"),
            snapshot.get("reply_to_external_id"),
            snapshot.get("attachments") or {},
            None,
        )
        if item.source_type == "message" and item.side == "unknown":
            incomplete = True
        if _is_request(item):
            break
        if (
            item.source_type == "message"
            and item.side == "vedita"
            and item.reply_to_external_id in (None, anchor.external_id)
        ):
            if not item.text.strip() or item.attachments:
                incomplete = True
                continue
            return item.sent_at, incomplete
    return None, incomplete
