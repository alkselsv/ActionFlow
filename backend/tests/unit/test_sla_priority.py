from datetime import UTC, datetime
from types import SimpleNamespace

from actionflow.application.rules import evaluate_unanswered
from actionflow.detectors.contracts import (
    Candidate,
    DetectorResult,
    Evidence,
    InputMessage,
    Subject,
)
from actionflow.domain.priority import PrioritySignals, calculate_priority
from actionflow.domain.sla import (
    SlaPolicy,
    add_working_minutes,
    evaluate_first_response,
)

WORK_HOURS = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "09:00",
    "end": "18:00",
}
POLICY = SlaPolicy(
    timezone="Europe/Moscow",
    work_hours=WORK_HOURS,
    closed_dates=(),
    first_response_minutes=60,
)


def test_sla_counts_only_working_minutes():
    monday_morning = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)  # 09:30 Moscow
    assert add_working_minutes(
        monday_morning,
        60,
        timezone=POLICY.timezone,
        work_hours=POLICY.work_hours,
        closed_dates=POLICY.closed_dates,
    ) == datetime(2026, 9, 14, 7, 30, tzinfo=UTC)


def test_sla_moves_to_next_workday_and_skips_closed_dates():
    friday_evening = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)  # 17:30 Moscow
    assert add_working_minutes(
        friday_evening,
        60,
        timezone=POLICY.timezone,
        work_hours=POLICY.work_hours,
        closed_dates=(),
    ) == datetime(2026, 9, 21, 6, 30, tzinfo=UTC)

    assert add_working_minutes(
        friday_evening,
        60,
        timezone=POLICY.timezone,
        work_hours=POLICY.work_hours,
        closed_dates=("2026-09-21",),
    ) == datetime(2026, 9, 22, 6, 30, tzinfo=UTC)


def test_sla_does_not_consume_weekend_time():
    saturday = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)  # 12:00 Moscow
    assert add_working_minutes(
        saturday,
        60,
        timezone=POLICY.timezone,
        work_hours=POLICY.work_hours,
        closed_dates=(),
    ) == datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


def test_reaction_at_boundary_is_timely_and_absence_at_boundary_is_breach():
    anchor = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)
    due = datetime(2026, 9, 14, 7, 30, tzinfo=UTC)

    met = evaluate_first_response(anchor, due, due, POLICY)
    assert met.state == "met"
    assert met.due_at == due

    late = evaluate_first_response(
        anchor, due.replace(second=1), due.replace(second=1), POLICY
    )
    assert late.state == "breached"

    pending = evaluate_first_response(
        anchor, None, datetime(2026, 9, 14, 7, 29, tzinfo=UTC), POLICY
    )
    assert pending.state == "pending"

    breached = evaluate_first_response(anchor, None, due, POLICY)
    assert breached.state == "breached"


def test_uncertain_data_is_not_reported_as_a_confident_breach():
    anchor = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)
    result = evaluate_first_response(
        anchor,
        None,
        datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
        POLICY,
        limitations=("unknown_side",),
    )
    assert result.state == "uncertain"
    assert result.reason["limitations"] == ["unknown_side"]


def test_missing_setting_is_not_a_calculated_sla():
    result = evaluate_first_response(
        datetime(2026, 9, 14, 6, 30, tzinfo=UTC),
        None,
        datetime(2026, 9, 14, 9, 0, tzinfo=UTC),
        None,
    )
    assert result.state == "not_calculated"
    assert result.due_at is None


def test_priority_rules_cover_all_three_contracts():
    as_of = datetime(2026, 9, 15, 10, 35, tzinfo=UTC)

    breach = calculate_priority(
        PrioritySignals(finding_type="unanswered", as_of=as_of, sla_state="breached")
    )
    assert breach.priority == "high"
    assert "first_response_sla_breached" in breach.reason["reasons"]

    pending = calculate_priority(
        PrioritySignals(finding_type="unanswered", as_of=as_of, sla_state="pending")
    )
    assert pending.priority == "medium"

    uncertain = calculate_priority(
        PrioritySignals(
            finding_type="unanswered",
            as_of=as_of,
            sla_state="breached",
            limitations=("unknown_side",),
        )
    )
    assert uncertain.priority == "medium"
    assert "data_limitations_require_manual_review" in uncertain.reason["reasons"]

    future = calculate_priority(
        PrioritySignals(
            finding_type="commitment",
            as_of=as_of,
            commitment_due_at=datetime(2026, 9, 15, 15, 0, tzinfo=UTC),
            commitment_status="open",
        )
    )
    assert future.priority == "low"

    overdue = calculate_priority(
        PrioritySignals(
            finding_type="commitment",
            as_of=as_of,
            commitment_due_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
            commitment_status="open",
        )
    )
    assert overdue.priority == "high"

    no_deadline = calculate_priority(
        PrioritySignals(
            finding_type="commitment", as_of=as_of, commitment_status="open"
        )
    )
    assert no_deadline.priority == "medium"

    complaint = calculate_priority(
        PrioritySignals(finding_type="complaint", as_of=as_of)
    )
    assert complaint.priority == "medium"

    risk = calculate_priority(
        PrioritySignals(finding_type="complaint", as_of=as_of, explicit_risk=True)
    )
    assert risk.priority == "high"


def test_risk_outweighs_a_future_commitment_deadline():
    result = calculate_priority(
        PrioritySignals(
            finding_type="commitment",
            as_of=datetime(2026, 9, 15, 10, 35, tzinfo=UTC),
            commitment_due_at=datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
            commitment_status="open",
            explicit_risk=True,
        )
    )
    assert result.priority == "high"


def _candidate(limitations=()):
    return Candidate(
        candidate_key="sha256:synthetic",
        subject=Subject(
            kind="message",
            anchor_message_id="message-1",
            anchor_revision_id="revision-1",
        ),
        description="Synthetic candidate",
        confidence=0.8,
        priority_hint="unknown",
        suggested_action="review",
        evidence=[
            Evidence(
                role="anchor",
                message_id="message-1",
                revision_id="revision-1",
                quote="Synthetic text",
            )
        ],
        limitations=list(limitations),
    )


def test_priority_consumes_all_three_agreed_detector_contracts():
    as_of = datetime(2026, 9, 15, 10, 35, tzinfo=UTC)
    results = [
        DetectorResult(
            finding_type=finding_type,
            candidates=[_candidate(("unknown_side",))],
        )
        for finding_type in ("unanswered", "commitment", "complaint")
    ]

    evaluations = [
        calculate_priority(
            PrioritySignals(
                finding_type=result.finding_type,
                as_of=as_of,
                sla_state="breached",
                commitment_due_at=datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
                commitment_status="open",
                explicit_risk=result.finding_type == "complaint",
                limitations=tuple(result.candidates[0].limitations),
            )
        )
        for result in results
    ]

    assert [item.priority for item in evaluations] == ["medium", "medium", "high"]
    assert all(
        "data_limitations_require_manual_review" in item.reason["reasons"]
        for item in evaluations
    )


def _message(message_id, side, sent_at):
    return InputMessage(
        message_id=message_id,
        revision_id=f"revision-{message_id}",
        external_id=f"external-{message_id}",
        text="Synthetic text",
        sent_at=sent_at,
        role="window",
        side=side,
        source_type="message",
        reply_to_external_id=None,
        attachments={},
        author_name=None,
    )


def _settings():
    return SimpleNamespace(
        id="settings-1",
        timezone="Europe/Moscow",
        work_hours=WORK_HOURS,
        calendar={"closed_dates": []},
        first_response_minutes=60,
    )


def test_customer_message_is_not_counted_as_vedita_first_response():
    anchor = _message("anchor", "customer", datetime(2026, 9, 15, 9, 30, tzinfo=UTC))
    customer_reply = _message(
        "customer-reply", "customer", datetime(2026, 9, 15, 9, 45, tzinfo=UTC)
    )
    candidate = _candidate()
    candidate = candidate.model_copy(
        update={
            "evidence": [
                Evidence(
                    role="anchor",
                    message_id="anchor",
                    revision_id="revision-anchor",
                ),
                Evidence(
                    role="supporting",
                    message_id="customer-reply",
                    revision_id="revision-customer-reply",
                ),
            ]
        }
    )
    result = evaluate_unanswered(
        SimpleNamespace(
            as_of=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
            settings_version_id="settings-1",
        ),
        _settings(),
        anchor,
        candidate,
        {"anchor": anchor, "customer-reply": customer_reply},
    )

    assert result.first_response_at is None
    assert result.sla_state == "breached"
    assert result.priority == "high"


def test_unknown_side_keeps_sla_uncertain():
    anchor = _message("anchor", "unknown", datetime(2026, 9, 15, 9, 30, tzinfo=UTC))
    candidate = _candidate(("unknown_side",)).model_copy(
        update={
            "evidence": [
                Evidence(
                    role="anchor",
                    message_id="anchor",
                    revision_id="revision-anchor",
                )
            ]
        }
    )
    result = evaluate_unanswered(
        SimpleNamespace(
            as_of=datetime(2026, 9, 15, 11, 0, tzinfo=UTC),
            settings_version_id="settings-1",
        ),
        _settings(),
        anchor,
        candidate,
        {"anchor": anchor},
    )

    assert result.sla_state == "uncertain"
    assert result.priority == "medium"

