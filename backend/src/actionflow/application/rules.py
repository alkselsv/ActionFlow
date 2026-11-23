from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from actionflow.detectors.contracts import Candidate, InputMessage
from actionflow.domain.priority import PrioritySignals, calculate_priority
from actionflow.domain.sla import SlaPolicy, evaluate_first_response
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import as_utc
from actionflow.persistence.findings import Commitment
from actionflow.persistence.project_settings import SettingsVersion


@dataclass(frozen=True)
class RuleResult:
    priority: str
    priority_reason: dict
    sla_due_at: datetime | None
    sla_state: str
    sla_reason: dict
    first_response_at: datetime | None
    settings_version_id: str | None
    commitment_due_at: datetime | None = None
    commitment_status: str | None = None


def settings_at(db, run, settings, applicable_at):
    if not (run.idempotency_key or "").startswith(
        ("telegram-event:", "telegram-schedule:")
    ):
        return settings
    return db.scalar(
        select(SettingsVersion)
        .where(
            SettingsVersion.project_id == run.project_id,
            SettingsVersion.created_at <= min(as_utc(applicable_at), as_utc(run.as_of)),
        )
        .order_by(SettingsVersion.version_no.desc())
        .limit(1)
    )


def policy_from_settings(settings: SettingsVersion | None) -> SlaPolicy | None:
    if settings is None:
        return None
    calendar = settings.calendar or {}
    return SlaPolicy(
        timezone=settings.timezone,
        work_hours=settings.work_hours,
        closed_dates=tuple(calendar.get("closed_dates", [])),
        first_response_minutes=settings.first_response_minutes,
    )


def _has_gap(run, start, end):
    return any(
        as_utc(datetime.fromisoformat(gap["start_at"])) <= as_utc(end)
        and (
            not gap["end_at"]
            or as_utc(datetime.fromisoformat(gap["end_at"])) > as_utc(start)
        )
        for gap in (getattr(run, "limitations", None) or {}).get("telegram_gaps", ())
    )


def evaluate_unanswered(
    run: AnalysisRun,
    settings: SettingsVersion | None,
    anchor: InputMessage,
    candidate: Candidate,
    input_messages: dict[str, InputMessage],
) -> RuleResult:
    first_response_at = None
    response_times = []
    for evidence in candidate.evidence:
        if evidence.role != "supporting":
            continue
        message = input_messages.get(evidence.message_id)
        if (
            message is not None
            and message.side == "vedita"
            and message.sent_at >= anchor.sent_at
        ):
            response_times.append(message.sent_at)
    if response_times:
        first_response_at = min(response_times)

    limitations = tuple(candidate.limitations)
    policy = policy_from_settings(settings)
    sla = evaluate_first_response(
        anchor.sent_at,
        first_response_at,
        run.as_of,
        policy,
        limitations,
    )
    if (
        sla.due_at
        and (first_response_at is None or first_response_at > sla.due_at)
        and _has_gap(run, anchor.sent_at, min(sla.due_at, as_utc(run.as_of)))
    ):
        limitations = (*limitations, "telegram_coverage_gap")
        sla = evaluate_first_response(
            anchor.sent_at, first_response_at, run.as_of, policy, limitations
        )
    priority = calculate_priority(
        PrioritySignals(
            finding_type="unanswered",
            as_of=run.as_of,
            sla_state=sla.state,
            explicit_risk=candidate.explicit_risk,
            limitations=limitations,
        )
    )
    return RuleResult(
        priority=priority.priority,
        priority_reason=priority.reason,
        sla_due_at=sla.due_at,
        sla_state=sla.state,
        sla_reason={
            **sla.reason,
            "settings_version_id": settings.id if settings else None,
        },
        first_response_at=sla.first_response_at,
        settings_version_id=settings.id if settings is not None else None,
    )


def evaluate_commitment(
    run: AnalysisRun,
    settings: SettingsVersion | None,
    commitment: Commitment,
    candidate: Candidate,
    anchor_at: datetime | None = None,
) -> RuleResult:
    terms = candidate.commitment
    explicit_risk = bool(
        (terms.explicit_risk if terms is not None else False)
        or commitment.explicit_risk
    )
    version_id = getattr(commitment, "due_settings_version_id", None) or (
        settings.id if settings else None
    )
    gap = anchor_at is not None and _has_gap(run, anchor_at, run.as_of)
    limitations = tuple(candidate.limitations)
    if gap:
        limitations = (*limitations, "telegram_coverage_gap")
    priority = calculate_priority(
        PrioritySignals(
            finding_type="commitment",
            as_of=run.as_of,
            commitment_due_at=commitment.due_at,
            commitment_status=commitment.status,
            commitment_due_uncertain=commitment.due_uncertain or gap,
            explicit_risk=explicit_risk,
            limitations=limitations,
        )
    )
    return RuleResult(
        priority=priority.priority,
        priority_reason={
            **priority.reason,
            "settings_version_id": version_id,
        },
        sla_due_at=None,
        sla_state="not_calculated",
        sla_reason={
            "rule_version": "sla.v1",
            "reason": "not_applicable",
            "finding_type": "commitment",
            "limitations": list(limitations),
        },
        first_response_at=None,
        settings_version_id=version_id,
        commitment_due_at=commitment.due_at,
        commitment_status=commitment.status,
    )


def evaluate_complaint(
    run: AnalysisRun,
    settings: SettingsVersion | None,
    candidate: Candidate,
) -> RuleResult:
    priority = calculate_priority(
        PrioritySignals(
            finding_type="complaint",
            as_of=run.as_of,
            explicit_risk=candidate.explicit_risk,
            limitations=tuple(candidate.limitations),
        )
    )
    return RuleResult(
        priority=priority.priority,
        priority_reason=priority.reason,
        sla_due_at=None,
        sla_state="not_calculated",
        sla_reason={
            "rule_version": "sla.v1",
            "reason": "not_applicable",
            "finding_type": "complaint",
            "limitations": list(candidate.limitations),
        },
        first_response_at=None,
        settings_version_id=settings.id if settings is not None else None,
    )
