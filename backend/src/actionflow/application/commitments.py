from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from actionflow.application.rules import RuleResult, evaluate_commitment
from actionflow.application.unanswered import load_detector_request
from actionflow.detectors.contracts import (
    Candidate as CandidateModel,
)
from actionflow.detectors.contracts import (
    Detector,
    DetectorResult,
    DetectorTimeout,
    InputMessage,
    InvalidDetectorOutput,
    Usage,
)
from actionflow.detectors.validation import CandidateValidation, validate_result
from actionflow.domain.commitments import parse_due
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import as_utc, utc_now
from actionflow.persistence.findings import (
    Candidate,
    CandidateEvidence,
    Commitment,
    CommitmentEvent,
    Finding,
    FindingObservation,
)
from actionflow.persistence.project_settings import (
    SettingsVersion,
)

TERMINAL = {"succeeded", "failed"}


@dataclass(frozen=True)
class CommitmentSummary:
    candidate_count: int
    finding_count: int
    invalid_candidate_count: int
    duration_ms: int
    usage: Usage


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


def _store_evidence(
    db: Session, candidate: Candidate, validation: CandidateValidation
) -> None:
    if validation.status == "invalid":
        return
    seen = set()
    for evidence in validation.candidate.evidence:
        key = (
            evidence.role,
            evidence.message_id,
            evidence.revision_id,
            evidence.quote,
        )
        if key in seen:
            continue
        seen.add(key)
        db.add(
            CandidateEvidence(
                candidate_id=candidate.id,
                role=evidence.role,
                message_id=evidence.message_id,
                revision_id=evidence.revision_id,
                quote=evidence.quote,
                quote_hash=sha256(
                    " ".join((evidence.quote or "").split()).encode()
                ).hexdigest()
                if evidence.quote is not None
                else None,
            )
        )


def _due_for(
    anchor: InputMessage,
    text: str,
    settings: SettingsVersion | None,
):
    if settings is None:
        return parse_due(
            "",
            anchor.sent_at,
            timezone="UTC",
            work_hours={"weekdays": [], "start": "09:00", "end": "18:00"},
        )
    calendar = settings.calendar or {}
    return parse_due(
        text,
        anchor.sent_at,
        timezone=settings.timezone,
        work_hours=settings.work_hours,
        closed_dates=tuple(calendar.get("closed_dates", [])),
    )


def _update_message(
    candidate: CandidateModel,
    messages: dict[str, InputMessage],
) -> InputMessage | None:
    for evidence in reversed(candidate.evidence):
        if evidence.role == "supporting":
            return messages.get(evidence.message_id)
    return None


def _add_event(
    db: Session,
    commitment: Commitment,
    kind: str,
    message: InputMessage | None,
    *,
    due_at: datetime | None = None,
    previous_due_at: datetime | None = None,
    uncertain: bool = False,
) -> None:
    exists = db.scalar(
        select(CommitmentEvent).where(
            CommitmentEvent.commitment_id == commitment.id,
            CommitmentEvent.kind == kind,
            CommitmentEvent.message_id
            == (message.message_id if message is not None else None),
            CommitmentEvent.revision_id
            == (message.revision_id if message is not None else None),
        )
    )
    if exists is not None:
        return
    db.add(
        CommitmentEvent(
            commitment_id=commitment.id,
            kind=kind,
            message_id=message.message_id if message is not None else None,
            revision_id=message.revision_id if message is not None else None,
            due_at=due_at,
            previous_due_at=previous_due_at,
            at=message.sent_at if message is not None else utc_now(),
            uncertain=uncertain,
        )
    )


def _upsert_commitment(
    db: Session,
    run: AnalysisRun,
    candidate: CandidateModel,
    anchor: InputMessage,
    update: InputMessage | None,
    settings: SettingsVersion | None,
) -> Commitment:
    terms = candidate.commitment
    event = terms.event if terms is not None else "promise"
    expression = (
        terms.due_expression
        if terms is not None and terms.due_expression
        else (anchor.text if event == "promise" else "")
    )
    due = _due_for(anchor, expression, settings)
    explicit_risk = bool(terms.explicit_risk) if terms is not None else False
    assignee = (
        terms.assignee_name
        if terms is not None and terms.assignee_name
        else anchor.author_name
    )
    subject_text = (
        terms.subject_text
        if terms is not None and terms.subject_text
        else anchor.text
    )

    commitment = db.scalar(
        select(Commitment)
        .where(
            Commitment.project_id == run.project_id,
            Commitment.anchor_message_id == anchor.message_id,
        )
        .with_for_update()
    )
    now = utc_now()
    if commitment is None:
        commitment = Commitment(
            project_id=run.project_id,
            anchor_message_id=anchor.message_id,
            anchor_revision_id=anchor.revision_id,
            subject_text=subject_text,
            assignee_name=assignee,
            status="open",
            due_at=due.due_at,
            due_uncertain=due.uncertain,
            explicit_risk=explicit_risk,
            created_at=now,
            updated_at=now,
        )
        db.add(commitment)
        db.flush()
        _add_event(
            db,
            commitment,
            "promise",
            anchor,
            due_at=due.due_at,
            uncertain=due.uncertain,
        )
        return commitment

    commitment.updated_at = now
    commitment.explicit_risk = commitment.explicit_risk or explicit_risk
    if assignee:
        commitment.assignee_name = assignee
    if subject_text:
        commitment.subject_text = subject_text

    if event == "promise":
        # Re-observing the original promise in a later window must not reopen a
        # completed commitment or rewrite its prior deadline.
        if commitment.due_at is None and due.due_at is not None:
            commitment.due_at = due.due_at
            commitment.due_uncertain = due.uncertain
        return commitment

    if event == "repeat":
        _add_event(db, commitment, "repeat", update)
        return commitment

    previous_due = commitment.due_at
    if event == "reschedule":
        commitment.due_at = due.due_at
        commitment.due_uncertain = due.uncertain
        commitment.status = "open"
        _add_event(
            db,
            commitment,
            "reschedule",
            update,
            due_at=due.due_at,
            previous_due_at=previous_due,
            uncertain=due.uncertain,
        )
    elif event == "cancel":
        commitment.status = "cancelled"
        _add_event(db, commitment, "cancel", update)
    elif event == "fulfill":
        commitment.status = "fulfilled"
        _add_event(db, commitment, "fulfill", update)
    return commitment


def _upsert_finding(
    db: Session,
    run: AnalysisRun,
    candidate_row: Candidate,
    validation: CandidateValidation,
    commitment: Commitment,
    rules: RuleResult,
) -> None:
    finding = db.scalar(
        select(Finding)
        .where(
            Finding.project_id == run.project_id,
            Finding.finding_type == "commitment",
            Finding.subject_kind == "commitment",
            Finding.subject_id == commitment.id,
        )
        .with_for_update()
    )
    now = utc_now()
    candidate = validation.candidate
    if finding is None:
        finding = Finding(
            project_id=run.project_id,
            finding_type="commitment",
            subject_kind="commitment",
            subject_id=commitment.id,
            anchor_message_id=commitment.anchor_message_id,
            anchor_revision_id=commitment.anchor_revision_id,
            status="new",
            description=candidate.description,
            suggested_action=candidate.suggested_action,
            confidence=candidate.confidence,
            latest_candidate_id=candidate_row.id,
            current_run_id=run.id,
            version=1,
            created_at=now,
            updated_at=now,
            priority=rules.priority,
            priority_reason=rules.priority_reason,
            sla_state=rules.sla_state,
            sla_reason=rules.sla_reason,
            commitment_due_at=rules.commitment_due_at,
            commitment_status=rules.commitment_status,
        )
        db.add(finding)
        db.flush()
    else:
        current_run = (
            db.get(AnalysisRun, finding.current_run_id)
            if finding.current_run_id
            else None
        )
        if _newer_run(current_run, run):
            finding.anchor_message_id = commitment.anchor_message_id
            finding.anchor_revision_id = commitment.anchor_revision_id
            finding.description = candidate.description
            finding.suggested_action = candidate.suggested_action
            finding.confidence = candidate.confidence
            finding.latest_candidate_id = candidate_row.id
            finding.current_run_id = run.id
            finding.priority = rules.priority
            finding.priority_reason = rules.priority_reason
            finding.sla_state = rules.sla_state
            finding.sla_reason = rules.sla_reason
            finding.commitment_due_at = rules.commitment_due_at
            finding.commitment_status = rules.commitment_status
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
                commitment_due_at=rules.commitment_due_at,
                commitment_status=rules.commitment_status,
                settings_version_id=rules.settings_version_id,
            )
        )
    else:
        # A promise and its update can both arrive in one run. Keep one
        # observation for the run but reflect the latest state and evidence.
        observation.candidate_id = candidate_row.id
        observation.summary = candidate.description
        observation.priority = rules.priority
        observation.priority_reason = rules.priority_reason
        observation.sla_due_at = rules.sla_due_at
        observation.sla_state = rules.sla_state
        observation.sla_reason = rules.sla_reason
        observation.first_response_at = rules.first_response_at
        observation.commitment_due_at = rules.commitment_due_at
        observation.commitment_status = rules.commitment_status
        observation.settings_version_id = rules.settings_version_id


def analyze_commitments(
    sessions,
    detector: Detector,
    run_id: str,
    *,
    deadline: datetime | None = None,
) -> CommitmentSummary:
    started = perf_counter()
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL:
            return CommitmentSummary(
                candidate_count=run.candidate_count or 0,
                finding_count=run.finding_count or 0,
                invalid_candidate_count=run.invalid_candidate_count or 0,
                duration_ms=run.detector_duration_ms or 0,
                usage=Usage.model_validate(run.usage or {}),
            )
        if run.stage not in (
            "input_ready",
            "detector",
            "unanswered_ready",
            "commitment_ready",
        ):
            raise ValueError("Analysis input is not prepared")
        request = load_detector_request(db, run)

    raw = detector.detect(request)
    result = _as_result(raw)
    if result.finding_type != "commitment":
        raise InvalidDetectorOutput(
            f"Unsupported finding type: {result.finding_type}"
        )
    if deadline is not None and utc_now() >= deadline:
        raise DetectorTimeout("Detector deadline elapsed")
    validations = validate_result(
        result,
        request.messages,
        allowed_anchor_sides=("vedita",),
        expected_subject_kind="commitment",
        allow_context_anchor=True,
    )
    duration_ms = int((perf_counter() - started) * 1000)

    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL:
            return CommitmentSummary(
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
        run.stage = "commitment_ready"
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
            anchor = input_by_id[anchor_id]
            candidate_row = Candidate(
                run_id=run.id,
                project_id=run.project_id,
                finding_type="commitment",
                candidate_key=candidate_model.candidate_key,
                subject_kind="commitment",
                subject_id=anchor_id,
                anchor_message_id=anchor_id,
                anchor_revision_id=anchor_revision,
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
                update = _update_message(candidate_model, input_by_id)
                commitment = _upsert_commitment(
                    db, run, candidate_model, anchor, update, settings
                )
                candidate_row.subject_id = commitment.id
                rules = evaluate_commitment(
                    run, settings, commitment, candidate_model
                )
                _upsert_finding(
                    db, run, candidate_row, validation, commitment, rules
                )
        run.finding_count = db.scalar(
            select(func.count())
            .select_from(FindingObservation)
            .where(FindingObservation.run_id == run.id)
        ) or 0
        run.candidate_count = db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.run_id == run.id)
        ) or 0
        run.invalid_candidate_count = db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(
                Candidate.run_id == run.id,
                Candidate.validation_status == "invalid",
            )
        ) or 0
        old_limitations = dict(run.limitations or {})
        old_limitations["commitment"] = {
            "candidate_count": len(result.candidates),
            "valid_candidate_count": sum(
                validation.status == "valid" for validation in validations
            ),
            "invalid_candidate_count": sum(
                validation.status == "invalid" for validation in validations
            ),
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

    return CommitmentSummary(
        candidate_count=len(result.candidates),
        finding_count=run.finding_count or 0,
        invalid_candidate_count=sum(
            validation.status == "invalid" for validation in validations
        ),
        duration_ms=duration_ms,
        usage=result.usage,
    )
