"""Explainable first-experiment priority rules (AF-002)."""

from dataclasses import dataclass
from datetime import UTC, datetime

DATA_UNCERTAINTY = {
    "unknown_side",
    "missing_text",
    "possible_attachment_answer",
    "unparsed_content",
}


@dataclass(frozen=True)
class PrioritySignals:
    finding_type: str
    as_of: datetime
    status: str = "new"
    sla_state: str = "not_calculated"
    commitment_due_at: datetime | None = None
    commitment_status: str | None = None
    explicit_risk: bool = False
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriorityResult:
    priority: str
    reason: dict


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def calculate_priority(signals: PrioritySignals) -> PriorityResult:
    limitations = list(signals.limitations)
    uncertain = any(item in DATA_UNCERTAINTY for item in limitations)
    reasons: list[str] = []

    if signals.explicit_risk:
        priority = "high"
        reasons.append("explicit_contract_or_operational_risk")
    elif signals.finding_type == "unanswered":
        if signals.sla_state == "breached" and not uncertain:
            priority = "high"
            reasons.append("first_response_sla_breached")
        else:
            priority = "medium"
            reasons.append(
                "uncertain_or_pending_sla"
                if signals.sla_state in ("uncertain", "not_calculated")
                else "open_request_waiting_for_response"
            )
    elif signals.finding_type == "commitment":
        due = (
            _utc(signals.commitment_due_at)
            if signals.commitment_due_at is not None
            else None
        )
        active = signals.commitment_status in (None, "open", "pending")
        if due is not None and active and _utc(signals.as_of) > due:
            priority = "medium" if uncertain else "high"
            reasons.append("commitment_deadline_missed")
        elif due is not None and active:
            priority = "low"
            reasons.append("commitment_deadline_in_future")
        else:
            priority = "medium"
            reasons.append("commitment_without_reliable_deadline")
    elif signals.finding_type == "complaint":
        priority = "medium"
        reasons.append("complaint_requires_manager_review")
    else:
        priority = "medium"
        reasons.append("unknown_finding_type")

    if uncertain:
        reasons.append("data_limitations_require_manual_review")

    return PriorityResult(
        priority=priority,
        reason={
            "rule_version": "priority.v1",
            "finding_type": signals.finding_type,
            "priority": priority,
            "reasons": reasons,
            "sla_state": signals.sla_state,
            "commitment_due_at": signals.commitment_due_at.isoformat()
            if signals.commitment_due_at
            else None,
            "explicit_risk": signals.explicit_risk,
            "limitations": limitations,
            "evaluated_at": _utc(signals.as_of).isoformat(),
        },
    )
