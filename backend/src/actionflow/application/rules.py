from dataclasses import dataclass
from datetime import datetime

from actionflow.detectors.contracts import Candidate, InputMessage
from actionflow.domain.priority import PrioritySignals, calculate_priority
from actionflow.domain.sla import SlaPolicy, evaluate_first_response
from actionflow.persistence.analysis import AnalysisRun
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
    sla = evaluate_first_response(
        anchor.sent_at,
        first_response_at,
        run.as_of,
        policy_from_settings(settings),
        limitations,
    )
    priority = calculate_priority(
        PrioritySignals(
            finding_type="unanswered",
            as_of=run.as_of,
            sla_state=sla.state,
            limitations=limitations,
        )
    )
    return RuleResult(
        priority=priority.priority,
        priority_reason=priority.reason,
        sla_due_at=sla.due_at,
        sla_state=sla.state,
        sla_reason=sla.reason,
        first_response_at=sla.first_response_at,
        settings_version_id=settings.id if settings is not None else None,
    )


def evaluate_commitment(
    run: AnalysisRun,
    settings: SettingsVersion | None,
    commitment: Commitment,
    candidate: Candidate,
) -> RuleResult:
    terms = candidate.commitment
    explicit_risk = bool(
        (terms.explicit_risk if terms is not None else False)
        or commitment.explicit_risk
    )
    priority = calculate_priority(
        PrioritySignals(
            finding_type="commitment",
            as_of=run.as_of,
            commitment_due_at=commitment.due_at,
            commitment_status=commitment.status,
            commitment_due_uncertain=commitment.due_uncertain,
            explicit_risk=explicit_risk,
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
            "finding_type": "commitment",
        },
        first_response_at=None,
        settings_version_id=settings.id if settings is not None else None,
        commitment_due_at=commitment.due_at,
        commitment_status=commitment.status,
    )
