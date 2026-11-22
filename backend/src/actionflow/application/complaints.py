from dataclasses import dataclass
from datetime import datetime
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import func, select

from actionflow.application.rules import evaluate_complaint
from actionflow.application.run_results import record_detector_result
from actionflow.application.unanswered import (
    _store_evidence,
    _upsert_finding,
    load_detector_request,
)
from actionflow.detectors.contracts import (
    Detector,
    DetectorResult,
    DetectorTimeout,
    InvalidDetectorOutput,
    Usage,
)
from actionflow.detectors.validation import validate_result
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import utc_now
from actionflow.persistence.findings import Candidate, FindingObservation
from actionflow.persistence.project_settings import SettingsVersion

TERMINAL = {"succeeded", "failed"}


@dataclass(frozen=True)
class ComplaintSummary:
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


def analyze_complaints(
    sessions,
    detector: Detector,
    run_id: str,
    *,
    deadline: datetime | None = None,
) -> ComplaintSummary:
    started = perf_counter()
    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL or "complaint" in (run.limitations or {}):
            return ComplaintSummary(
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
            "complaints_ready",
        ):
            raise ValueError("Analysis input is not prepared")
        request = load_detector_request(db, run)

    raw = detector.detect(request)
    result = _as_result(raw)
    if result.finding_type != "complaint":
        raise InvalidDetectorOutput(
            f"Unsupported finding type: {result.finding_type}"
        )
    if deadline is not None and utc_now() >= deadline:
        raise DetectorTimeout("Detector deadline elapsed")
    validations = validate_result(
        result,
        request.messages,
        allowed_anchor_sides=("customer", "unknown"),
        expected_subject_kind="complaint_episode",
        allow_context_anchor=True,
    )
    duration_ms = int((perf_counter() - started) * 1000)

    with sessions.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Unknown analysis run")
        if run.status in TERMINAL or "complaint" in (run.limitations or {}):
            return ComplaintSummary(
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
        run.stage = "complaints_ready"
        settings = (
            db.get(SettingsVersion, run.settings_version_id)
            if run.settings_version_id
            else None
        )
        rejected: list[dict] = []
        input_revisions = {
            message.message_id: message.revision_id for message in request.messages
        }
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
                finding_type="complaint",
                candidate_key=candidate_model.candidate_key,
                subject_kind="complaint_episode",
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
                rules = evaluate_complaint(run, settings, candidate_model)
                _upsert_finding(
                    db, run, candidate_row, validation, rules
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
        old_limitations["complaint"] = {
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
        record_detector_result(
            run, detector, result, duration_ms, old_limitations["complaint"]
        )

    return ComplaintSummary(
        candidate_count=len(result.candidates),
        finding_count=run.finding_count or 0,
        invalid_candidate_count=sum(
            validation.status == "invalid" for validation in validations
        ),
        duration_ms=duration_ms,
        usage=result.usage,
    )
