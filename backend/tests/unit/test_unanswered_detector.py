from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from actionflow.detectors.contracts import (
    Candidate,
    DetectorRequest,
    DetectorResult,
    Evidence,
    InputMessage,
    Subject,
)
from actionflow.detectors.local_baseline import LocalBaselineDetector
from actionflow.detectors.validation import validate_result


def message(
    number: int,
    side: str,
    text: str,
    *,
    role: str = "window",
    attachments: dict | None = None,
) -> InputMessage:
    return InputMessage(
        message_id=f"m{number:04d}",
        revision_id=f"r{number:04d}",
        external_id=f"n-{number:03d}",
        text=text,
        sent_at=datetime(2026, 9, 14, 9, number, tzinfo=UTC),
        role=role,
        side=side,
        source_type="message",
        reply_to_external_id=None,
        attachments=attachments or {},
        author_name=side,
    )


def request(*messages: InputMessage) -> DetectorRequest:
    return DetectorRequest(
        run_id="run",
        project_id="project",
        period_start=datetime(2026, 9, 14, tzinfo=UTC),
        period_end=datetime(2026, 9, 16, tzinfo=UTC),
        as_of=datetime(2026, 9, 16, tzinfo=UTC),
        messages=tuple(messages),
    )


def candidate(anchor: InputMessage, quote: str | None = None) -> Candidate:
    return Candidate(
        candidate_key="candidate-1",
        subject=Subject(
            kind="message",
            anchor_message_id=anchor.message_id,
            anchor_revision_id=anchor.revision_id,
        ),
        description="Клиент ждёт ответа",
        confidence=0.8,
        suggested_action="reply",
        evidence=[
            Evidence(
                role="anchor",
                message_id=anchor.message_id,
                revision_id=anchor.revision_id,
                quote=anchor.text if quote is None else quote,
            )
        ],
    )


def test_local_baseline_finds_only_unresolved_request():
    question = message(1, "customer", "Когда будет готов отчёт?")
    acknowledgement = message(2, "vedita", "Проверю и вернусь с ответом.")
    resolved_question = message(3, "customer", "Можно обновить инструкцию?")
    resolved = message(4, "vedita", "Готово, направила новую инструкцию.")
    thanks = message(5, "customer", "Спасибо!")

    result = LocalBaselineDetector().detect(
        request(question, acknowledgement, resolved_question, resolved, thanks)
    )

    assert [item.subject.anchor_message_id for item in result.candidates] == [
        question.message_id
    ]
    unresolved = result.candidates[0]
    assert "first_reaction_without_resolution" in unresolved.limitations
    assert unresolved.subject.anchor_message_id != resolved_question.message_id
    assert result.usage.cost_usd is None
    assert result.finding_type == "unanswered"


def test_customer_message_is_not_a_vedita_answer_and_multiple_questions_are_kept():
    first = message(1, "customer", "Почему смета не открывается?")
    customer_answer = message(2, "customer", "Я нашёл старую версию.")
    second = message(3, "customer", "Когда пришлёте новую?")
    employee = message(4, "vedita", "Проверяю отправку, вернусь с подтверждением.")

    result = LocalBaselineDetector().detect(
        request(first, customer_answer, second, employee)
    )

    assert {item.subject.anchor_message_id for item in result.candidates} == {
        first.message_id,
        second.message_id,
    }
    assert all(
        all(
            evidence.message_id != customer_answer.message_id
            for evidence in item.evidence
            if evidence.role == "supporting"
        )
        for item in result.candidates
    )


@pytest.mark.parametrize(
    ("question", "answer", "is_open"),
    [
        ("Когда будет готова демонстрация?", "Покажем завтра в 11:00.", False),
        ("Когда будет готова демонстрация?", "Завтра в 11:00.", False),
        ("Когда будет готова демонстрация?", "Уточню завтра в 11:00.", True),
        ("Когда будет готова демонстрация?", "Возможно, завтра в 11:00.", True),
        ("Пришлите демонстрацию.", "Покажем завтра в 11:00.", True),
    ],
)
def test_concrete_schedule_answers_only_a_question_about_timing(
    question, answer, is_open
):
    result = LocalBaselineDetector().detect(
        request(message(1, "customer", question), message(2, "vedita", answer))
    )
    assert bool(result.candidates) is is_open


def test_blocked_operation_is_an_open_request_with_linked_escalation():
    blocked = message(1, "customer", "Интеграция снова не передаёт заказы.")
    reaction = message(2, "vedita", "Проверяем журналы, вернусь с результатом.")
    escalation = message(
        3, "customer", "Если не восстановите обмен, мы остановим работы."
    )
    [item] = (
        LocalBaselineDetector()
        .detect(request(blocked, reaction, escalation))
        .candidates
    )
    assert item.subject.anchor_message_id == blocked.message_id
    assert item.explicit_risk
    assert [evidence.message_id for evidence in item.evidence] == [
        blocked.message_id,
        reaction.message_id,
        escalation.message_id,
    ]


def test_attachment_response_is_flagged_as_incomplete():
    question = message(1, "customer", "Пришлите, пожалуйста, отчёт.")
    attachment = message(
        2,
        "vedita",
        "",
        attachments={"file": "report.pdf", "media_type": "document"},
    )

    result = LocalBaselineDetector().detect(request(question, attachment))

    assert len(result.candidates) == 1
    assert "possible_attachment_answer" in result.candidates[0].limitations
    assert result.candidates[0].evidence[1].quote is None


def test_media_only_message_without_text_is_not_guessed():
    media = message(
        1,
        "customer",
        "",
        attachments={"photo": "screen.jpg", "media_type": "photo"},
    )
    result = LocalBaselineDetector().detect(request(media))
    assert result.candidates == []


def test_validation_accepts_valid_evidence_and_rejects_foreign_or_bad_quote():
    anchor = message(1, "customer", "Когда будет отчёт?")
    response = message(2, "vedita", "Проверю.")
    valid = DetectorResult(finding_type="unanswered", candidates=[candidate(anchor)])
    assert validate_result(valid, (anchor, response))[0].status == "valid"

    foreign = valid.model_copy(deep=True)
    foreign.candidates[0].evidence.append(
        Evidence(role="supporting", message_id="foreign", revision_id="foreign-r")
    )
    validation = validate_result(foreign, (anchor, response))[0]
    assert validation.status == "invalid"
    assert any("evidence_not_in_input" in error for error in validation.errors)

    wrong_quote = valid.model_copy(deep=True)
    wrong_quote.candidates[0].evidence[0].quote = "Такого текста нет"
    validation = validate_result(wrong_quote, (anchor, response))[0]
    assert validation.status == "invalid"
    assert any("quote_not_in_revision" in error for error in validation.errors)

    wrong_revision = valid.model_copy(deep=True)
    wrong_revision.candidates[0].evidence[0].revision_id = "other-revision"
    validation = validate_result(wrong_revision, (anchor, response))[0]
    assert validation.status == "invalid"
    assert any("evidence_revision_mismatch" in error for error in validation.errors)


def test_unknown_fields_are_rejected_by_the_detector_schema():
    with pytest.raises(ValidationError):
        DetectorResult.model_validate(
            {
                "finding_type": "unanswered",
                "candidates": [],
                "unexpected": True,
            }
        )


def test_vedita_message_cannot_be_an_unanswered_anchor():
    employee = message(1, "vedita", "Когда будет отчёт?")
    result = LocalBaselineDetector().detect(request(employee))
    assert result.candidates == []
