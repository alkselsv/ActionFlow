from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from actionflow.application.rules import RuleResult, evaluate_unanswered
from actionflow.detectors.contracts import (
    Detector,
    DetectorRequest,
    DetectorResult,
    DetectorTimeout,
    InputMessage,
    InvalidDetectorOutput,
    Usage,
)
from actionflow.detectors.validation import CandidateValidation, validate_result
from actionflow.persistence.analysis import AnalysisInputMessage, AnalysisRun
from actionflow.persistence.database import as_utc, utc_now
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
    SettingsVersion,
)

TERMINAL = {"succeeded", "failed"}


@dataclass(frozen=True)
class UnansweredSummary:
    candidate_count: int
    finding_count: int
    invalid_candidate_count: int
    duration_ms: int
    usage: Usage


def load_detector_request(db: Session, run: AnalysisRun) -> DetectorRequest:
    rows = db.execute(
        select(AnalysisInputMessage, MessageRevision, Message, Participant)
        .join(
            MessageRevision,
            MessageRevision.id == AnalysisInputMessage.revision_id,
        )
        .join(Message, Message.id == AnalysisInputMessage.message_id)
        .outerjoin(Participant, Participant.id == Message.participant_id)
        .where(AnalysisInputMessage.run_id == run.id)
        .order_by(Message.sent_at, Message.external_id)
    ).all()
    messages = []
    for item, revision, message, participant in rows:
        snapshot = revision.snapshot
        messages.append(
            InputMessage(
                message_id=message.id,
                revision_id=revision.id,
                external_id=message.external_id,
                text=str(snapshot.get("text", "")),
                sent_at=as_utc(item.sent_at),
                role=item.role,
                side=item.participant_side,
                source_type=str(snapshot.get("source_type", "message")),
                reply_to_external_id=snapshot.get("reply_to_external_id"),
                attachments=snapshot.get("attachments", {}),
                author_name=participant.display_name if participant else None,
            )
        )
    return DetectorRequest(
        run_id=run.id,
        project_id=run.project_id,
        period_start=as_utc(run.period_start),
        period_end=as_utc(run.period_end),
        as_of=as_utc(run.as_of),
        messages=tuple(messages),
    )


def _as_result(value) -> DetectorResult:
    if isinstance(value, DetectorResult):
        return value
    try:
        return DetectorResult.model_validate(value)
    except ValidationError as failure:
        raise InvalidDetectorOutput(str(failure)) from failure


def _newer_run(current: AnalysisRun | None, incoming: AnalysisRun) -> bool:
    if current is None:
        return True
    current_as_of = as_utc(current.as_of)
    incoming_as_of = as_utc(incoming.as_of)
    if incoming_as_of != current_as_of:
        return incoming_as_of > current_as_of
    return as_utc(incoming.created_at) >= as_utc(current.created_at)


def _store_evidence(db: Session, candidate: Candidate, validation: CandidateValidation):
    if validation.status == "invalid":
        # Foreign or malformed evidence must stay in diagnostics; storing it
        # would violate the project/revision foreign keys.
        return
    seen = set()
    for evidence in validation.candidate.evidence:
        quote = evidence.quote
        digest = (
            sha256(" ".join(quote.split()).encode()).hexdigest()
            if quote is not None
            else None
        )
        key = (evidence.role, evidence.message_id, evidence.revision_id, digest)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            CandidateEvidence(
                candidate_id=candidate.id,
                role=evidence.role,
                message_id=evidence.message_id,
                revision_id=evidence.revision_id,
                quote=quote,
                quote_hash=digest,
            )
        )


def _upsert_finding(
    db: Session,
    run: AnalysisRun,
    candidate_row: Candidate,
    validation: CandidateValidation,
    rules: RuleResult,
) -> bool:
    finding = db.scalar(
        select(Finding)
        .where(
            Finding.project_id == run.project_id,
            Finding.finding_type == candidate_row.finding_type,
            Finding.subject_kind == candidate_row.subject_kind,
            Finding.subject_id == candidate_row.subject_id,
        )
        .with_for_update()
    )
    now = utc_now()
    candidate = validation.candidate
    if finding is None:
        finding = Finding(
            project_id=run.project_id,
            finding_type=candidate_row.finding_type,
            subject_kind=candidate_row.subject_kind,
            subject_id=candidate_row.subject_id,
            anchor_message_id=candidate_row.anchor_message_id,
            anchor_revision_id=candidate_row.anchor_revision_id,
            status="new",
            description=candidate.description,
            suggested_action=candidate.suggested_action,
            confidence=candidate.confidence,
            latest_candidate_id=candidate_row.id,
            current_run_id=run.id,
            version=1,
            created_at=now,
            updated_at=now,
            first_response_at=rules.first_response_at,
            sla_due_at=rules.sla_due_at,
            sla_state=rules.sla_state,
            sla_reason=rules.sla_reason,
            priority=rules.priority,
            priority_reason=rules.priority_reason,
        )
        db.add(finding)
        db.flush()
    else:
        current_run = (
            db.get(AnalysisRun, finding.current_run_id)
            if finding.current_run_id
            else None
        )
        changed = False
        if _newer_run(current_run, run):
            finding.anchor_message_id = candidate_row.anchor_message_id
            finding.anchor_revision_id = candidate_row.anchor_revision_id
            finding.description = candidate.description
            finding.suggested_action = candidate.suggested_action
            finding.confidence = candidate.confidence
            finding.latest_candidate_id = candidate_row.id
            finding.current_run_id = run.id
            finding.first_response_at = rules.first_response_at
            finding.sla_due_at = rules.sla_due_at
            finding.sla_state = rules.sla_state
            finding.sla_reason = rules.sla_reason
            finding.priority = rules.priority
            finding.priority_reason = rules.priority_reason
            changed = True
        if changed:
            finding.version += 1
            finding.updated_at = now

    observation = db.scalar(
        select(FindingObservation).where(
            FindingObservation.finding_id == finding.id,
            FindingObservation.run_id == run.id,
        )
    )
    if observation is None:
        db.add(
            FindingObservation(
                finding_id=finding.id,
                run_id=run.id,
                candidate_id=candidate_row.id,
                observed_at=now,
                summary=candidate.description,
                priority=rules.priority,
                priority_reason=rules.priority_reason,
                sla_due_at=rules.sla_due_at,
                sla_state=rules.sla_state,
                sla_reason=rules.sla_reason,
                first_response_at=rules.first_response_at,
                settings_version_id=rules.settings_version_id,
            )
        )
        return True
    return False


def analyze_unanswered(
    sessions,
    detector: Detector,
    run_id: str,
    *,
    deadline: datetime | None = None,
) -> UnansweredSummary:
    started = perf_counter()
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL:
            return UnansweredSummary(
                candidate_count=run.candidate_count or 0,
                finding_count=run.finding_count or 0,
                invalid_candidate_count=run.invalid_candidate_count or 0,
                duration_ms=run.detector_duration_ms or 0,
                usage=Usage.model_validate(run.usage or {}),
            )
        if run.stage not in ("input_ready", "detector"):
            raise ValueError("Analysis input is not prepared")
        request = load_detector_request(db, run)

    raw = detector.detect(request)
    result = _as_result(raw)
    if result.finding_type != "unanswered":
        raise InvalidDetectorOutput(
            f"Unsupported finding type: {result.finding_type}"
        )
    if deadline is not None and utc_now() >= deadline:
        raise DetectorTimeout("Detector deadline elapsed")
    validations = validate_result(result, request.messages)
    duration_ms = int((perf_counter() - started) * 1000)

    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL:
            return UnansweredSummary(
                candidate_count=run.candidate_count or 0,
                finding_count=run.finding_count or 0,
                invalid_candidate_count=run.invalid_candidate_count or 0,
                duration_ms=run.detector_duration_ms or 0,
                usage=Usage.model_validate(run.usage or {}),
            )

        run.provider = detector.provider
        run.model_id = detector.model_id
        run.detector_version = detector.detector_version
        run.prompt_version = detector.prompt_version
        run.schema_version = detector.schema_version
        run.model_params = detector.model_params
        run.usage = result.usage.model_dump(mode="json")
        run.cost_usd = (
            Decimal(str(result.usage.cost_usd))
            if result.usage.cost_usd is not None
            else None
        )
        run.detector_duration_ms = duration_ms
        run.candidate_count = len(result.candidates)
        run.invalid_candidate_count = sum(
            validation.status == "invalid" for validation in validations
        )
        run.stage = "detector"
        input_revisions = {
            message.message_id: message.revision_id for message in request.messages
        }
        input_by_id = {message.message_id: message for message in request.messages}
        settings = (
            db.get(SettingsVersion, run.settings_version_id)
            if run.settings_version_id
            else None
        )
        rejected: list[dict] = []
        for validation in validations:
            candidate_model = validation.candidate
            anchor_id = candidate_model.subject.anchor_message_id
            anchor_revision = candidate_model.subject.anchor_revision_id
            if input_revisions.get(anchor_id) != anchor_revision:
                rejected.append(
                    {
                        "candidate_key": candidate_model.candidate_key,
                        "errors": list(validation.errors)
                        or ["anchor_not_in_input"],
                    }
                )
                continue
            existing = db.scalar(
                select(Candidate).where(
                    Candidate.run_id == run.id,
                    Candidate.candidate_key == candidate_model.candidate_key,
                )
            )
            if existing is not None:
                continue
            candidate_row = Candidate(
                run_id=run.id,
                project_id=run.project_id,
                finding_type=result.finding_type,
                candidate_key=candidate_model.candidate_key,
                subject_kind=candidate_model.subject.kind,
                subject_id=validation.subject_id,
                anchor_message_id=candidate_model.subject.anchor_message_id,
                anchor_revision_id=candidate_model.subject.anchor_revision_id,
                description=candidate_model.description,
                confidence=candidate_model.confidence,
                priority_hint=candidate_model.priority_hint,
                suggested_action=candidate_model.suggested_action,
                limitations=candidate_model.limitations,
                validation_status=validation.status,
                validation_errors=list(validation.errors),
                raw_payload=candidate_model.model_dump(mode="json"),
            )
            db.add(candidate_row)
            db.flush()
            _store_evidence(db, candidate_row, validation)
            if validation.status == "valid" and run.apply_to_queue:
                rules = evaluate_unanswered(
                    run,
                    settings,
                    input_by_id[anchor_id],
                    candidate_model,
                    input_by_id,
                )
                _upsert_finding(db, run, candidate_row, validation, rules)
        run.finding_count = db.scalar(
            select(func.count())
            .select_from(FindingObservation)
            .where(FindingObservation.run_id == run.id)
        ) or 0
        stored_finding_count = run.finding_count
        old_limitations = dict(run.limitations or {})
        old_limitations["unanswered"] = {
            "candidate_count": len(result.candidates),
            "valid_candidate_count": sum(
                validation.status == "valid" for validation in validations
            ),
            "invalid_candidate_count": run.invalid_candidate_count,
            "limitations": sorted(
                {
                    limitation
                    for validation in validations
                    for limitation in validation.candidate.limitations
                }
            ),
            "model": detector.model_id,
            "cost_known": result.usage.cost_usd is not None,
            "rejected_candidates": rejected,
        }
        run.limitations = old_limitations

    return UnansweredSummary(
        candidate_count=len(result.candidates),
        finding_count=stored_finding_count,
        invalid_candidate_count=sum(
            validation.status == "invalid" for validation in validations
        ),
        duration_ms=duration_ms,
        usage=result.usage,
    )


def finalize_run(sessions, run_id: str) -> None:
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None or run.status in TERMINAL:
            return
        run.status = "succeeded"
        run.stage = (
            "findings_ready"
            if run.stage == "commitment_ready"
            else "unanswered_ready"
        )
        run.finished_at = utc_now()
