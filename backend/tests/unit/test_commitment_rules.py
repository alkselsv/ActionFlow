from datetime import UTC, datetime
from types import SimpleNamespace

from actionflow.application.rules import evaluate_commitment
from actionflow.detectors.contracts import DetectorRequest, InputMessage
from actionflow.detectors.local_commitments import LocalCommitmentDetector
from actionflow.domain.commitments import parse_due

WORK_HOURS = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "09:00",
    "end": "18:00",
}


def _due(text, sent_at, *, timezone="Europe/Moscow", closed=()):
    return parse_due(
        text,
        sent_at,
        timezone=timezone,
        work_hours=WORK_HOURS,
        closed_dates=closed,
    )


def _message(message_id, text, sent_at, *, side="vedita"):
    return InputMessage(
        message_id=message_id,
        revision_id=f"revision-{message_id}",
        external_id=f"external-{message_id}",
        text=text,
        sent_at=sent_at,
        role="window",
        side=side,
        source_type="message",
        reply_to_external_id=None,
        attachments={},
        author_name="Synthetic employee" if side == "vedita" else "Synthetic customer",
    )


def _detect(messages):
    return LocalCommitmentDetector().detect(
        DetectorRequest(
            run_id="run",
            project_id="project",
            period_start=datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
            period_end=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
            as_of=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
            messages=tuple(messages),
        )
    )


def test_relative_due_uses_local_date_and_explicit_time():
    sent = datetime(2026, 9, 14, 9, 12, tzinfo=UTC)  # 12:12 Moscow
    result = _due("Исправленную смету пришлю завтра до 12:00.", sent)
    assert result.due_at == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert not result.uncertain


def test_bare_today_uses_end_of_working_day():
    sent = datetime(2026, 9, 15, 6, 35, tzinfo=UTC)  # 09:35 Moscow
    result = _due("Срок сообщу сегодня.", sent)
    assert result.due_at == datetime(2026, 9, 15, 15, 0, tzinfo=UTC)


def test_explicit_time_and_calendar_duration():
    sent = datetime(2026, 9, 15, 10, 5, tzinfo=UTC)  # 13:05 Moscow
    assert _due("Подготовлю выгрузку сегодня до 17:30.", sent).due_at == datetime(
        2026, 9, 15, 14, 30, tzinfo=UTC
    )

    duration_sent = datetime(2026, 9, 15, 7, 10, tzinfo=UTC)
    assert _due(
        "Исправлю инструкцию в течение 2 часов.", duration_sent
    ).due_at == datetime(2026, 9, 15, 9, 10, tzinfo=UTC)


def test_explicit_calendar_date_and_unknown_weekday_text():
    sent = datetime(2026, 9, 15, 9, 20, tzinfo=UTC)
    explicit = _due("Пришлю до 16 сентября 09:00.", sent)
    assert explicit.due_at == datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
    assert not explicit.uncertain

    friday = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    result = _due("Подготовлю в понедельник", friday)
    assert result.due_at is None
    assert not result.uncertain


def test_related_risk_is_attached_to_the_commitment():
    result = _detect(
        [
            _message(
                "1",
                "Интеграция снова не передаёт заказы.",
                datetime(2026, 9, 15, 5, 55, tzinfo=UTC),
                side="customer",
            ),
            _message(
                "2",
                "Проверяем журналы, вернусь с результатом.",
                datetime(2026, 9, 15, 6, 5, tzinfo=UTC),
            ),
            _message(
                "3",
                "Если сегодня не восстановите обмен, остановим работы и пересмотрим договор.",
                datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
                side="customer",
            ),
        ]
    )
    assert result.candidates[0].commitment.explicit_risk is True
    assert result.candidates[0].commitment.event == "promise"


def test_detector_cancel_and_quote_are_distinguished():
    result = _detect(
        [
            _message(
                "1",
                "Подготовлю выгрузку завтра.",
                datetime(2026, 9, 15, 6, 5, tzinfo=UTC),
            ),
            _message(
                "2",
                "По словам клиента, пришлю другой файл.",
                datetime(2026, 9, 15, 6, 30, tzinfo=UTC),
            ),
            _message(
                "3",
                "Отменяю обещание по выгрузке.",
                datetime(2026, 9, 15, 7, 0, tzinfo=UTC),
            ),
        ]
    )
    assert [item.commitment.event for item in result.candidates] == [
        "promise",
        "cancel",
    ]
    assert result.candidates[0].commitment.assignee_name == "Synthetic employee"
    assert result.candidates[0].subject.anchor_message_id == "1"


def test_commitment_rule_uses_risk_and_unknown_deadline():
    candidate = _detect(
        [
            _message(
                "1",
                "Проверяем журналы, вернусь с результатом.",
                datetime(2026, 9, 15, 6, 5, tzinfo=UTC),
            ),
            _message(
                "2",
                "Остановим работы и пересмотрим договор.",
                datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
                side="customer",
            ),
        ]
    ).candidates[0]
    result = evaluate_commitment(
        SimpleNamespace(
            as_of=datetime(2026, 9, 15, 8, 35, tzinfo=UTC),
        ),
        None,
        SimpleNamespace(
            status="open",
            due_at=None,
            due_uncertain=False,
            explicit_risk=True,
        ),
        candidate,
    )
    assert result.priority == "high"
    assert "explicit_contract_or_operational_risk" in result.priority_reason[
        "reasons"
    ]
    assert result.commitment_status == "open"
    assert result.commitment_due_at is None


def test_detector_distinguishes_promise_reschedule_and_fulfillment():
    result = _detect(
        [
            _message(
                "1",
                "Подготовлю выгрузку сегодня до 17:30.",
                datetime(2026, 9, 15, 6, 5, tzinfo=UTC),
            ),
            _message(
                "2",
                "Перенесу выгрузку на завтра.",
                datetime(2026, 9, 15, 7, 0, tzinfo=UTC),
            ),
            _message(
                "3",
                "Готово, отправила выгрузку.",
                datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
            ),
        ]
    )
    assert [item.commitment.event for item in result.candidates] == [
        "promise",
        "reschedule",
        "fulfill",
    ]
    assert {item.commitment.event for item in result.candidates} == {
        "promise",
        "reschedule",
        "fulfill",
    }
    assert all(
        item.subject.anchor_message_id == "1" for item in result.candidates
    )


def test_repeat_keeps_original_anchor_and_ignores_past_or_assumption():
    repeated = _detect(
        [
            _message(
                "1",
                "Передала ошибку разработчику, срок сообщу сегодня.",
                datetime(2026, 9, 15, 6, 35, tzinfo=UTC),
            ),
            _message(
                "2",
                "Точный срок напишу позже сегодня.",
                datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
            ),
            _message(
                "3",
                "Запросила УПД у бухгалтерии.",
                datetime(2026, 9, 15, 10, 10, tzinfo=UTC),
            ),
            _message(
                "4",
                "Возможно, пришлю завтра.",
                datetime(2026, 9, 15, 10, 20, tzinfo=UTC),
            ),
        ]
    )
    assert [item.commitment.event for item in repeated.candidates] == [
        "promise",
        "repeat",
    ]
    assert repeated.candidates[1].subject.anchor_message_id == "1"
    assert repeated.candidates[1].commitment.due_expression is None
