"""Deterministic local baseline for Vedita commitments."""

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import ClassVar

from actionflow.detectors.contracts import (
    Candidate,
    CommitmentTerms,
    DetectorRequest,
    DetectorResult,
    Evidence,
    InputMessage,
    Subject,
    Usage,
)

MODEL_ID = "local-baseline-commitment-v1"
DETECTOR_VERSION = "local-commitment-v1"
PROMPT_VERSION = "commitment-rules-v1"
SCHEMA_VERSION = "detector_result.v1"

_PROMISE_MARKERS = (
    "пришлю",
    "подготовлю",
    "исправлю",
    "передам",
    "вернусь",
    "сообщу",
    "напишу",
    "покажем",
    "проверю",
    "сделаю",
    "направлю",
    "обновлю",
    "выполню",
    "предоставлю",
    "свяжусь",
    "уточню",
    "посмотрю",
    "отправлю",
    "продублирую",
    "запрошу",
    "выясню",
    "организую",
    "запланирую",
    "дам",
)
_RESULT_MARKERS = (
    "готово",
    "направила",
    "направил",
    "отправила",
    "отправил",
    "прислала",
    "прислал",
    "исправила",
    "исправил",
    "выполнила",
    "выполнил",
    "сделала",
    "сделал",
    "обновила",
    "обновил",
    "предоставила",
    "предоставил",
    "продублировалась",
    "по ошибке",
    "подтверждаю",
)
_RESCHEDULE_MARKERS = (
    "перенес",
    "перенос",
    "сдвин",
    "не успеваю",
    "не успеем",
)
_CANCEL_MARKERS = (
    "отменя",
    "отменил",
    "отменила",
    "отказываюсь",
    "снимаю",
    "не нужно",
    "не требуется",
)
_ASSUMPTION_MARKERS = (
    "возможно",
    "наверное",
    "предполож",
    "может быть",
    "если ",
)
_QUOTE_MARKERS = (
    "цитат",
    "по словам",
    "как сказал",
    "как сказала",
)
_RISK_MARKERS = (
    "останов",
    "пересмотр",
    "договор",
    "критич",
    "срыв",
    "простой",
)
_TOPICS = {
    "deadline": ("срок",),
    "estimate": ("смет", "расчёт", "расчет"),
    "instruction": ("инструкц",),
    "export": ("выгрузк",),
    "demo": ("демонстрац",),
    "result": ("результат", "журнал", "восстанов"),
    "observation": ("наблюден",),
    "upd": ("упд",),
}


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _topic(text: str) -> str:
    normalized = _normal(text)
    for topic, words in _TOPICS.items():
        if _has_any(normalized, words):
            return topic
    return "general"


def _snippet(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _quote(text: str) -> str | None:
    return text if len(text) <= 2000 else text[:2000]


def _is_vedita_message(message: InputMessage) -> bool:
    return (
        message.side == "vedita"
        and message.source_type == "message"
        and bool(message.text.strip())
    )


def _is_promise(text: str) -> bool:
    normalized = _normal(text)
    if not normalized:
        return False
    if _has_any(normalized, _QUOTE_MARKERS):
        return False
    if _has_any(normalized, _ASSUMPTION_MARKERS):
        return False
    return _has_any(normalized, _PROMISE_MARKERS)


def _has_result(text: str) -> bool:
    normalized = _normal(text)
    return _has_any(normalized, _RESULT_MARKERS) and not _is_promise(text)


def _has_reschedule(text: str) -> bool:
    return _has_any(_normal(text), _RESCHEDULE_MARKERS)


def _has_cancel(text: str) -> bool:
    return _has_any(_normal(text), _CANCEL_MARKERS)


def _due_hint(text: str) -> str | None:
    normalized = _normal(text)
    if re.search(
        r"сегодня|завтра|послезавтра|в течение|через|\d{1,2}:\d{2}|"
        r"января|февраля|марта|апреля|мая|июня|июля|августа|сентября|"
        r"октября|ноября|декабря",
        normalized,
    ):
        return text
    return None


def _risk_in_conversation(messages: list[InputMessage]) -> bool:
    return any(
        item.source_type == "message"
        and _has_any(_normal(item.text), _RISK_MARKERS)
        for item in messages
    )


@dataclass
class _TrackedCommitment:
    anchor: InputMessage
    topic: str
    due_expression: str | None
    subject_text: str
    explicit_risk: bool
    status: str = "open"


def _candidate_key(anchor: InputMessage, event: str, update: InputMessage | None):
    material = (
        f"commitment|{anchor.message_id}|{event}|"
        f"{update.message_id if update else '-'}"
    )
    return "commitment:" + sha256(material.encode()).hexdigest()[:40]


def _make_candidate(
    anchor: InputMessage,
    event: str,
    topic: str,
    update: InputMessage | None,
    due_expression: str | None,
    subject_text: str,
    explicit_risk: bool,
) -> Candidate:
    evidence = [
        Evidence(
            role="anchor",
            message_id=anchor.message_id,
            revision_id=anchor.revision_id,
            quote=_quote(anchor.text),
        )
    ]
    if update is not None and update.message_id != anchor.message_id:
        evidence.append(
            Evidence(
                role="supporting",
                message_id=update.message_id,
                revision_id=update.revision_id,
                quote=_quote(update.text),
            )
        )

    if event == "promise":
        description = (
            f"Vedita обещает: «{_snippet(anchor.text)}». "
            f"Срок: {_snippet(due_expression) if due_expression else 'не указан'}."
        )
    elif event == "repeat":
        description = (
            f"Vedita повторяет обещание: «{_snippet(anchor.text)}». "
            "Срок не меняется."
        )
    elif event == "reschedule":
        description = (
            f"Vedita переносит срок обещания «{_snippet(anchor.text)}»: "
            f"{_snippet(update.text if update else '')}."
        )
    elif event == "cancel":
        description = (
            f"Vedita отменяет обещание «{_snippet(anchor.text)}»: "
            f"{_snippet(update.text if update else '')}."
        )
    else:
        description = (
            f"Vedita сообщает об исполнении обещания «{_snippet(anchor.text)}»: "
            f"{_snippet(update.text if update else '')}."
        )

    return Candidate(
        candidate_key=_candidate_key(anchor, event, update),
        subject=Subject(
            kind="commitment",
            anchor_message_id=anchor.message_id,
            anchor_revision_id=anchor.revision_id,
            fragment=_snippet(anchor.text),
            discriminator=f"commitment:{topic}",
        ),
        description=description,
        confidence=0.82 if event == "promise" else 0.72,
        priority_hint="unknown",
        suggested_action="follow_up",
        evidence=evidence,
        commitment=CommitmentTerms(
            event=event,
            subject_text=subject_text,
            due_expression=due_expression,
            assignee_name=(update or anchor).author_name,
            explicit_risk=explicit_risk,
        ),
        limitations=[],
    )


class LocalCommitmentDetector:
    provider = "local"
    model_id = MODEL_ID
    detector_version = DETECTOR_VERSION
    prompt_version = PROMPT_VERSION
    schema_version = SCHEMA_VERSION
    model_params: ClassVar[dict] = {"strategy": "rules", "version": DETECTOR_VERSION}

    def detect(self, request: DetectorRequest) -> DetectorResult:
        messages = list(request.messages)
        candidates: list[Candidate] = []
        open_commitments: list[_TrackedCommitment] = []
        conversation_has_risk = _risk_in_conversation(messages)

        def latest(topic: str | None = None) -> _TrackedCommitment | None:
            if topic is not None:
                for item in reversed(open_commitments):
                    if item.status == "open" and item.topic == topic:
                        return item
            for item in reversed(open_commitments):
                if item.status == "open":
                    return item
            return None

        for message in messages:
            if not _is_vedita_message(message):
                continue
            text = message.text
            topic = _topic(text)
            existing = latest(topic)

            if _has_cancel(text) and existing is not None:
                existing.status = "cancelled"
                candidates.append(
                    _make_candidate(
                        existing.anchor,
                        "cancel",
                        existing.topic,
                        message,
                        None,
                        existing.subject_text,
                        existing.explicit_risk,
                    )
                )
                continue

            if _has_reschedule(text) and existing is not None:
                existing.due_expression = _due_hint(text)
                candidates.append(
                    _make_candidate(
                        existing.anchor,
                        "reschedule",
                        existing.topic,
                        message,
                        _due_hint(text),
                        existing.subject_text,
                        existing.explicit_risk,
                    )
                )
                continue

            if _has_result(text) and existing is not None:
                existing.status = "fulfilled"
                candidates.append(
                    _make_candidate(
                        existing.anchor,
                        "fulfill",
                        existing.topic,
                        message,
                        None,
                        existing.subject_text,
                        existing.explicit_risk,
                    )
                )
                continue

            if not _is_promise(text):
                continue

            if existing is not None and existing.topic == topic:
                candidates.append(
                    _make_candidate(
                        existing.anchor,
                        "repeat",
                        existing.topic,
                        message,
                        None,
                        existing.subject_text,
                        existing.explicit_risk,
                    )
                )
                continue

            tracked = _TrackedCommitment(
                anchor=message,
                topic=topic,
                due_expression=_due_hint(text),
                subject_text=_snippet(text),
                explicit_risk=conversation_has_risk,
            )
            open_commitments.append(tracked)
            candidates.append(
                _make_candidate(
                    message,
                    "promise",
                    topic,
                    None,
                    _due_hint(text),
                    tracked.subject_text,
                    tracked.explicit_risk,
                )
            )

        input_size = sum(len(item.text) for item in messages)
        output_size = sum(len(item.description) for item in candidates)
        return DetectorResult(
            schema_version=self.schema_version,
            finding_type="commitment",
            candidates=candidates,
            usage=Usage(
                input_tokens=max(0, input_size // 4),
                output_tokens=max(0, output_size // 4),
                total_tokens=max(0, (input_size + output_size) // 4),
                cost_usd=None,
                currency="USD",
                pricing_version=None,
            ),
            diagnostics=[
                "local_commitment_baseline",
                f"messages={len(messages)}",
            ],
        )
