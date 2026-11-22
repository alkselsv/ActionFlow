from datetime import UTC, datetime
from types import SimpleNamespace

from actionflow.application.rules import evaluate_complaint
from actionflow.detectors.contracts import DetectorRequest, InputMessage
from actionflow.detectors.local_complaints import LocalComplaintDetector


def _message(message_id, text, *, side="customer"):
    return InputMessage(
        message_id=message_id,
        revision_id=f"revision-{message_id}",
        external_id=f"external-{message_id}",
        text=text,
        sent_at=datetime(2026, 9, 15, 6, 0, tzinfo=UTC),
        role="window",
        side=side,
        source_type="message",
        reply_to_external_id=None,
        attachments={},
        author_name="Synthetic customer" if side == "customer" else "Synthetic employee",
    )


def _detect(messages):
    return LocalComplaintDetector().detect(
        DetectorRequest(
            run_id="run",
            project_id="project",
            period_start=datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
            period_end=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
            as_of=datetime(2026, 9, 16, 0, 0, tzinfo=UTC),
            messages=tuple(messages),
        )
    )


def test_repeated_failure_is_one_complaint_without_risk():
    result = _detect(
        [
            _message("1", "После вчерашнего обновления фильтр снова сбрасывается."),
            _message("2", "Мы уже третий раз сообщаем об этой ошибке."),
            _message(
                "3",
                "Такое качество нас не устраивает. Когда будет окончательное исправление?",
            ),
            _message("4", "Передала ошибку разработчику.", side="vedita"),
        ]
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.subject.anchor_message_id == "1"
    assert [item.message_id for item in candidate.evidence if item.role == "supporting"] == [
        "2",
        "3",
    ]
    assert candidate.explicit_risk is False
    assert candidate.limitations == []

    rules = evaluate_complaint(
        SimpleNamespace(as_of=datetime(2026, 9, 15, 10, 5, tzinfo=UTC)),
        None,
        candidate,
    )
    assert rules.priority == "medium"


def test_escalation_risk_raises_complaint_priority():
    result = _detect(
        [
            _message("1", "Интеграция снова не передаёт заказы."),
            _message("2", "Проверяем журналы, вернусь с результатом.", side="vedita"),
            _message(
                "3",
                "Если сегодня не восстановите обмен, мы остановим работы "
                "и потребуем пересмотреть договор.",
            ),
        ]
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.explicit_risk is True
    assert [item.message_id for item in candidate.evidence if item.role == "supporting"] == [
        "3"
    ]

    rules = evaluate_complaint(
        SimpleNamespace(as_of=datetime(2026, 9, 15, 8, 35, tzinfo=UTC)),
        None,
        candidate,
    )
    assert rules.priority == "high"
    assert "explicit_contract_or_operational_risk" in rules.priority_reason["reasons"]


def test_neutral_observation_and_quote_do_not_create_complaint():
    neutral = _detect(
        [
            _message("1", "Цвет новой кнопки выглядит непривычно."),
            _message("2", "Это не мешает работе."),
        ]
    )
    assert neutral.candidates == []

    quoted = _detect(
        [
            _message(
                "1",
                "По словам клиента, качество его не устраивает.",
            )
        ]
    )
    assert quoted.candidates == []


def test_unknown_customer_side_is_marked_as_limitation():
    result = _detect(
        [
            _message(
                "1",
                "Снова не работает интеграция, уже третий раз.",
                side="unknown",
            )
        ]
    )
    assert len(result.candidates) == 1
    assert "unknown_side" in result.candidates[0].limitations
    rules = evaluate_complaint(
        SimpleNamespace(as_of=datetime(2026, 9, 15, 9, 0, tzinfo=UTC)),
        None,
        result.candidates[0],
    )
    assert rules.priority == "medium"
