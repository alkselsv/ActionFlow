from fastapi import HTTPException, Query
from sqlalchemy import select

from actionflow.api.project_settings import require_project
from actionflow.persistence.analysis import (
    AnalysisInputMessage,
    AnalysisRun,
)
from actionflow.persistence.database import as_utc
from actionflow.persistence.findings import (
    Candidate,
    CandidateEvidence,
    Finding,
    FindingObservation,
)
from actionflow.persistence.project_settings import (
    Message,
    MessageRevision,
    Participant,
)


def _run(db, run_id):
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "Запуск не найден")
    return run


def _input_rows(db, run_id):
    return db.execute(
        select(AnalysisInputMessage, MessageRevision, Message, Participant)
        .join(
            MessageRevision,
            MessageRevision.id == AnalysisInputMessage.revision_id,
        )
        .join(Message, Message.id == AnalysisInputMessage.message_id)
        .outerjoin(Participant, Participant.id == Message.participant_id)
        .where(AnalysisInputMessage.run_id == run_id)
        .order_by(Message.sent_at, Message.external_id)
    ).all()


def _message_payload(item, revision, message, participant, *, role=None, quote=None):
    snapshot = revision.snapshot
    side = item.participant_side
    return {
        "message_id": message.id,
        "revision_id": revision.id,
        "external_id": message.external_id,
        "role": role,
        "quote": quote,
        "text": snapshot.get("text", ""),
        "sent_at": as_utc(item.sent_at),
        "author": {
            "id": participant.source_author_id if participant else None,
            "name": participant.display_name if participant else None,
            "side": None if snapshot.get("source_type") == "service" else side,
        },
        "source_type": snapshot.get("source_type", "message"),
        "attachments": snapshot.get("attachments", {}),
        "reply_to_external_id": snapshot.get("reply_to_external_id"),
    }


def _context(db, run_id, anchor_message_id, radius=3):
    rows = _input_rows(db, run_id)
    index = next(
        (
            position
            for position, (item, _revision, message, _participant) in enumerate(rows)
            if message.id == anchor_message_id
        ),
        None,
    )
    if index is None:
        return []
    start = max(0, index - radius)
    end = min(len(rows), index + radius + 1)
    return [
        {
            **_message_payload(item, revision, message, participant, role=item.role),
            "is_anchor": message.id == anchor_message_id,
        }
        for item, revision, message, participant in rows[start:end]
    ]


def _candidate_view(db, candidate):
    if candidate is None:
        return None
    evidence_rows = db.execute(
        select(
            CandidateEvidence,
            AnalysisInputMessage,
            MessageRevision,
            Message,
            Participant,
        )
        .join(
            AnalysisInputMessage,
            (AnalysisInputMessage.run_id == candidate.run_id)
            & (AnalysisInputMessage.message_id == CandidateEvidence.message_id),
        )
        .join(MessageRevision, MessageRevision.id == CandidateEvidence.revision_id)
        .join(Message, Message.id == CandidateEvidence.message_id)
        .outerjoin(Participant, Participant.id == Message.participant_id)
        .where(CandidateEvidence.candidate_id == candidate.id)
        .order_by(CandidateEvidence.role, Message.sent_at)
    ).all()
    evidence = [
        {
            **_message_payload(
                input_row, revision, message, participant, role=item.role
            ),
            "quote": item.quote,
        }
        for item, input_row, revision, message, participant in evidence_rows
    ]
    return {
        "candidate_id": candidate.id,
        "candidate_key": candidate.candidate_key,
        "finding_type": candidate.finding_type,
        "status": candidate.validation_status,
        "validation_errors": candidate.validation_errors,
        "description": candidate.description,
        "confidence": candidate.confidence,
        "priority_hint": candidate.priority_hint,
        "suggested_action": candidate.suggested_action,
        "limitations": candidate.limitations,
        "subject": {
            "kind": candidate.subject_kind,
            "anchor_message_id": candidate.anchor_message_id,
            "anchor_revision_id": candidate.anchor_revision_id,
        },
        "evidence": evidence,
    }


def _summary(finding, run=None, observation=None):
    priority = (observation.priority if observation else None) or finding.priority
    priority_reason = (
        observation.priority_reason if observation else None
    ) or finding.priority_reason
    due_at = (observation.sla_due_at if observation else None) or finding.sla_due_at
    sla_state = (observation.sla_state if observation else None) or finding.sla_state
    sla_reason = (observation.sla_reason if observation else None) or finding.sla_reason
    first_response_at = (
        (observation.first_response_at if observation else None)
        or finding.first_response_at
    )
    settings_version_id = (
        (observation.settings_version_id if observation else None)
        or (run.settings_version_id if run is not None else None)
    )
    return {
        "id": finding.id,
        "project_id": finding.project_id,
        "type": finding.finding_type,
        "status": finding.status,
        "priority": priority,
        "priority_reason": priority_reason,
        "description": finding.description,
        "confidence": finding.confidence,
        "sla": {
            "due_at": as_utc(due_at),
            "state": sla_state,
            "first_response_at": as_utc(first_response_at),
            "reason": sla_reason,
            "settings_version_id": settings_version_id,
        },
        "limitations": (sla_reason or {}).get("limitations", []),
        "anchor_message_id": finding.anchor_message_id,
        "current_run_id": finding.current_run_id,
        "run_id": run.id if run is not None else None,
        "as_of": as_utc(run.as_of) if run is not None else None,
        "version": finding.version,
        "updated_at": as_utc(finding.updated_at),
    }


def _history(db, finding_id):
    rows = db.execute(
        select(FindingObservation, AnalysisRun)
        .join(AnalysisRun, AnalysisRun.id == FindingObservation.run_id)
        .where(FindingObservation.finding_id == finding_id)
        .order_by(AnalysisRun.created_at, FindingObservation.id)
    ).all()
    return [
        {
            "run_id": observation.run_id,
            "candidate_id": observation.candidate_id,
            "observed_at": as_utc(observation.observed_at),
            "as_of": as_utc(run.as_of),
            "summary": observation.summary,
            "priority": observation.priority,
            "priority_reason": observation.priority_reason,
            "sla": {
                "due_at": as_utc(observation.sla_due_at),
                "state": observation.sla_state,
                "first_response_at": as_utc(observation.first_response_at),
                "reason": observation.sla_reason,
                "settings_version_id": observation.settings_version_id,
            },
        }
        for observation, run in rows
    ]


def _finding(db, finding_id, run_id=None):
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(404, "Карточка не найдена")
    observation = None
    if run_id:
        observation = db.scalar(
            select(FindingObservation).where(
                FindingObservation.finding_id == finding.id,
                FindingObservation.run_id == run_id,
            )
        )
        if observation is None:
            raise HTTPException(404, "Карточка не найдена в этом запуске")
    elif finding.current_run_id:
        observation = db.scalar(
            select(FindingObservation).where(
                FindingObservation.finding_id == finding.id,
                FindingObservation.run_id == finding.current_run_id,
            )
        )
    selected_run_id = (
        observation.run_id if observation is not None else finding.current_run_id
    )
    run = db.get(AnalysisRun, selected_run_id) if selected_run_id else None
    candidate_id = (
        observation.candidate_id if observation is not None else finding.latest_candidate_id
    )
    candidate = db.get(Candidate, candidate_id) if candidate_id else None
    candidate_data = _candidate_view(db, candidate)
    first_reaction_at = None
    if candidate is not None:
        supporting = db.scalar(
            select(Message.sent_at)
            .join(CandidateEvidence, CandidateEvidence.message_id == Message.id)
            .where(
                CandidateEvidence.candidate_id == candidate.id,
                CandidateEvidence.role == "supporting",
            )
            .order_by(Message.sent_at)
            .limit(1)
        )
        first_reaction_at = as_utc(supporting)
    return {
        **_summary(finding, run, observation),
        "description": candidate.description if candidate else finding.description,
        "recommendation": candidate.suggested_action
        if candidate
        else finding.suggested_action,
        "confidence": candidate.confidence if candidate else finding.confidence,
        "priority_reason": (
            observation.priority_reason
            if observation is not None and observation.priority_reason
            else finding.priority_reason
        ),
        "subject": {
            "kind": finding.subject_kind,
            "id": finding.subject_id,
            "anchor_message_id": finding.anchor_message_id,
            "anchor_revision_id": finding.anchor_revision_id,
        },
        "limitations": (candidate.limitations if candidate else []),
        "candidate": candidate_data,
        "evidence": candidate_data["evidence"] if candidate_data else [],
        "surrounding": _context(db, selected_run_id, finding.anchor_message_id)
        if selected_run_id
        else [],
        "history": _history(db, finding.id),
        "detector": {
            "provider": run.provider,
            "model_id": run.model_id,
            "detector_version": run.detector_version,
            "prompt_version": run.prompt_version,
            "schema_version": run.schema_version,
            "duration_ms": run.detector_duration_ms,
            "usage": run.usage,
            "cost_usd": float(run.cost_usd) if run and run.cost_usd is not None else None,
        }
        if run
        else None,
        "first_response_at": (
            observation.first_response_at
            if observation is not None and observation.first_response_at
            else first_reaction_at
        ),
    }


def _project_findings(
    db,
    project_id,
    *,
    finding_type=None,
    status=None,
    priority=None,
    sla_state=None,
    run_id=None,
    limit=50,
    cursor=None,
):
    require_project(db, project_id)
    selected_run = None
    if run_id:
        selected_run = _run(db, run_id)
        query = (
            select(Finding, FindingObservation)
            .join(
                FindingObservation,
                FindingObservation.finding_id == Finding.id,
            )
            .where(
                Finding.project_id == project_id,
                FindingObservation.run_id == run_id,
            )
        )
        if priority:
            query = query.where(FindingObservation.priority == priority)
        if sla_state:
            query = query.where(FindingObservation.sla_state == sla_state)
    else:
        query = select(Finding).where(Finding.project_id == project_id)
        if priority:
            query = query.where(Finding.priority == priority)
        if sla_state:
            query = query.where(Finding.sla_state == sla_state)
    if finding_type:
        query = query.where(Finding.finding_type == finding_type)
    if status:
        query = query.where(Finding.status == status)
    query = query.order_by(Finding.updated_at.desc(), Finding.id.desc())
    if cursor:
        anchor = db.get(Finding, cursor)
        if anchor is None or anchor.project_id != project_id:
            raise HTTPException(400, "Некорректный курсор очереди")
        query = query.where(
            (Finding.updated_at < anchor.updated_at)
            | (
                (Finding.updated_at == anchor.updated_at)
                & (Finding.id < anchor.id)
            )
        )
    if run_id:
        rows = db.execute(query.limit(limit + 1)).all()
    else:
        rows = [(item, None) for item in db.scalars(query.limit(limit + 1))]
    items = rows[:limit]
    return {
        "items": [
            _summary(
                finding,
                selected_run
                or db.get(AnalysisRun, finding.current_run_id),
                observation,
            )
            for finding, observation in items
        ],
        "next_cursor": items[-1][0].id if len(rows) > limit else None,
    }


def register_finding_routes(projects, router, Db, session_required, origin_required):
    @projects.get("/projects/{project_id}/findings")
    def project_findings(
        project_id: str,
        type: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        sla: str | None = None,
        run_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
        db=Db,
    ):
        return _project_findings(
            db,
            project_id,
            finding_type=type,
            status=status,
            priority=priority,
            sla_state=sla,
            run_id=run_id,
            limit=limit,
            cursor=cursor,
        )

    @projects.get("/projects/{project_id}/findings/counts")
    def project_finding_counts(
        project_id: str, run_id: str | None = None, db=Db
    ):
        require_project(db, project_id)
        if run_id:
            _run(db, run_id)
            rows = db.execute(
                select(Finding, FindingObservation)
                .join(
                    FindingObservation,
                    FindingObservation.finding_id == Finding.id,
                )
                .where(
                    Finding.project_id == project_id,
                    FindingObservation.run_id == run_id,
                )
            ).all()
        else:
            rows = [(item, None) for item in db.scalars(
                select(Finding).where(Finding.project_id == project_id)
            )]
        by_status = {}
        by_priority = {}
        by_type = {}
        by_sla = {}
        open_count = 0
        sla_breaches = 0
        for finding, observation in rows:
            priority = (observation.priority if observation else None) or finding.priority
            sla_state = (
                observation.sla_state if observation else None
            ) or finding.sla_state
            by_status[finding.status] = by_status.get(finding.status, 0) + 1
            by_priority[priority] = by_priority.get(priority, 0) + 1
            by_type[finding.finding_type] = by_type.get(finding.finding_type, 0) + 1
            by_sla[sla_state] = by_sla.get(sla_state, 0) + 1
            open_count += finding.status in ("new", "confirmed", "snoozed")
            sla_breaches += sla_state == "breached"
        return {
            "total": len(rows),
            "open": open_count,
            "sla_breaches": sla_breaches,
            "by_status": by_status,
            "by_priority": by_priority,
            "by_type": by_type,
            "by_sla": by_sla,
        }

    @router.get("/findings/{finding_id}", dependencies=[session_required])
    def finding_card(finding_id: str, run_id: str | None = None, db=Db):
        return _finding(db, finding_id, run_id)

    @router.get("/analyses/{run_id}/findings", dependencies=[session_required])
    def run_findings(run_id: str, db=Db):
        run = _run(db, run_id)
        rows = db.execute(
            select(Finding, FindingObservation)
            .join(
                FindingObservation,
                FindingObservation.finding_id == Finding.id,
            )
            .where(FindingObservation.run_id == run.id)
            .order_by(Finding.updated_at.desc())
        ).all()
        return {
            "items": [
                _summary(finding, run, observation)
                for finding, observation in rows
            ]
        }

    @router.get("/analyses/{run_id}/candidates", dependencies=[session_required])
    def run_candidates(run_id: str, db=Db):
        run = _run(db, run_id)
        rows = list(
            db.scalars(
                select(Candidate)
                .where(Candidate.run_id == run.id)
                .order_by(Candidate.created_at, Candidate.id)
            )
        )
        return {"items": [_candidate_view(db, item) for item in rows]}
