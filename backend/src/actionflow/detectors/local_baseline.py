"""Local deterministic baseline for the first unanswered detector.

This adapter is deliberately not a remote LLM. AF-004 currently forbids external
providers, while issue #13 needs a working, reproducible detector. The adapter
implements the same structured contract as a future model and reports
``cost_usd = null`` because no paid call happens.
"""

import re
from hashlib import sha256
from typing import ClassVar

from actionflow.detectors.contracts import (
    Candidate,
    DetectorRequest,
    DetectorResult,
    Evidence,
    InputMessage,
    Subject,
    Usage,
)

MODEL_ID = "local-baseline-unanswered-v1"
DETECTOR_VERSION = "local-unanswered-v3"
PROMPT_VERSION = "unanswered-rules-v3"
SCHEMA_VERSION = "detector_result.v1"

_QUESTION_WORDS = (
    "?",
    "когда",
    "почему",
    "зачем",
    "как ",
    "где ",
    "куда",
    "что ",
    "кто ",
    "какой",
    "какие",
    "сколько",
    "можно",
    "нужн",
    "нужен",
    "нужно",
    "прошу",
    "пришлите",
    "подготовьте",
    "обновите",
    "исправьте",
    "сообщите",
    "подскажите",
    "уточните",
    "дайте",
    "сделайте",
    "есть ли",
    "будет ли",
)
_ACKNOWLEDGEMENTS = (
    "проверю",
    "проверяю",
    "вернусь",
    "передала",
    "передал",
    "сообщу",
    "напишу",
    "уточню",
    "посмотрю",
    "принял",
    "приняла",
    "взял",
    "взяла",
    "в работе",
    "в работу",
    "срок сообщу",
    "позже",
    "ожидайте",
    "понимаю",
    "понимаем",
    "спасибо",
    "благодар",
    "хорошо",
    "ждём",
    "ожидаем",
    "подготовлю",
    "исправлю",
    "сделаю",
    "пришлю",
    "направлю",
    "в течение",
    "сегодня",
    "завтра",
    "вижу",
)
_STRONG_RESULTS = (
    "готово",
    "направ",
    "присла",
    "отправ",
    "выполн",
    "исправлен",
    "обновлен",
    "предостав",
    "во вложении",
    "продублир",
    "результат",
    "файл приложен",
)
_FUTURE_MARKERS = (
    "пришлю",
    "подготовлю",
    "сделаю",
    "исправлю",
    "обновлю",
    "направлю",
    "сообщу",
    "напишу",
    "проверю",
    "проверяю",
    "вернусь",
    "уточню",
    "позже",
    "завтра",
    "сегодня",
    "в течение",
)
_SKIP_PHRASES = (
    "спасибо",
    "благодар",
    "доброе утро",
    "добрый день",
    "добрый вечер",
    "хорошо",
    "ок",
    "понял",
    "поняла",
    "получили",
    "получил",
    "ждём",
    "ожидаем",
)
_TIMING_QUESTION = re.compile(r"(?:когда\b|во сколько\b)[^?]*\?")
_BLOCKED_OPERATION = re.compile(
    r"\bне (?:переда[её]т|передают|выгружа[её]т|выгружают|принима[её]т|принимают)\s+"
    r"(?:заказы|платежи|данные|документы)\b"
)
_ESCALATION = re.compile(
    r"\b(?:остановим работы|приостановим работы|пересмотр(?:еть|а) договор\w*)\b"
)
_SCHEDULE_ANSWER = re.compile(
    r"(?:(?:покажем|провед[её]м|начн[её]м|будет готов[ао]?) )?"
    r"(?:сегодня|завтра|послезавтра) в (?:[01]?\d|2[0-3]):[0-5]\d[.!]?"
)


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _is_request(message: InputMessage) -> bool:
    text = _normal(message.text)
    if not text or message.source_type != "message":
        return False
    if message.side not in ("customer", "unknown"):
        return False
    if "?" not in text and len(text) <= 60 and _has_any(text, _SKIP_PHRASES):
        return False
    if _BLOCKED_OPERATION.search(text):
        return True
    if _has_any(text, ("пока не получили", "не работает", "снова сбрасывается")):
        return False
    return _has_any(text, _QUESTION_WORDS)


def _is_substantive(anchor: InputMessage, message: InputMessage) -> bool:
    normalized = _normal(message.text)
    if not normalized:
        return False
    if message.attachments:
        # A document can be the answer, but its content is not available to the
        # detector. The caller records this as an incompleteness signal.
        return False
    if _TIMING_QUESTION.fullmatch(_normal(anchor.text)) and _SCHEDULE_ANSWER.fullmatch(
        normalized
    ):
        return True
    completed = _has_any(normalized, _STRONG_RESULTS)
    future = _has_any(normalized, _FUTURE_MARKERS)
    return completed and not future


def _is_acknowledgement(text: str) -> bool:
    normalized = _normal(text)
    if not normalized:
        return False
    return _has_any(normalized, _ACKNOWLEDGEMENTS) and not _has_any(
        normalized, _STRONG_RESULTS
    )


def _snippet(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _quote(text: str) -> str | None:
    if not text.strip():
        return None
    return text if len(text) <= 2000 else text[:2000]


def _key(anchor: InputMessage, response: InputMessage | None) -> str:
    material = (
        f"unanswered|{anchor.message_id}|{response.message_id if response else '-'}"
    )
    return "unanswered:" + sha256(material.encode()).hexdigest()[:40]


def _candidate(
    anchor: InputMessage,
    responses: list[InputMessage],
    unknown_after: bool,
    escalation: InputMessage | None,
) -> Candidate | None:
    if any(_is_substantive(anchor, item) for item in responses):
        return None

    first = responses[0] if responses else None
    attachment_response = next(
        (item for item in responses if item.attachments),
        None,
    )
    limitations: list[str] = []
    if first is not None:
        limitations.append("first_reaction_without_resolution")
    else:
        limitations.append("no_vedita_response")
    if attachment_response is not None:
        limitations.append("possible_attachment_answer")
    if unknown_after:
        limitations.append("unknown_side")
    if not anchor.text.strip():
        limitations.append("unparsed_content")

    snippet = _snippet(anchor.text) if anchor.text.strip() else "сообщение без текста"
    if attachment_response is not None:
        description = (
            f"Клиент ждёт решения: «{snippet}». Vedita отправила вложение; "
            "содержимое ответа недоступно, полноту нужно проверить."
        )
    elif first is not None:
        description = (
            f"Клиент ждёт решения: «{snippet}». Vedita только подтвердила "
            "получение или пообещала вернуться, содержательного ответа нет."
        )
    else:
        description = (
            f"Клиент ждёт ответа: «{snippet}». Содержательного ответа Vedita "
            "в доступном срезе нет."
        )

    evidence = [
        Evidence(
            role="anchor",
            message_id=anchor.message_id,
            revision_id=anchor.revision_id,
            quote=_quote(anchor.text),
        )
    ]
    for item in responses[:3]:
        evidence.append(
            Evidence(
                role="supporting",
                message_id=item.message_id,
                revision_id=item.revision_id,
                quote=_quote(item.text),
            )
        )
    if escalation is not None:
        evidence.append(
            Evidence(
                role="supporting",
                message_id=escalation.message_id,
                revision_id=escalation.revision_id,
                quote=_quote(escalation.text),
            )
        )

    confidence = 0.8 if "?" in anchor.text else 0.68
    if anchor.side == "unknown":
        confidence = min(confidence, 0.5)
    if attachment_response is not None:
        confidence = min(confidence, 0.58)

    return Candidate(
        candidate_key=_key(anchor, first),
        subject=Subject(
            kind="message",
            anchor_message_id=anchor.message_id,
            anchor_revision_id=anchor.revision_id,
            fragment=_snippet(anchor.text) if anchor.text.strip() else None,
            discriminator="customer-request",
        ),
        description=description,
        confidence=confidence,
        priority_hint="unknown",
        suggested_action="reply",
        explicit_risk=escalation is not None,
        evidence=evidence,
        limitations=limitations,
    )


class LocalBaselineDetector:
    provider = "local"
    model_id = MODEL_ID
    detector_version = DETECTOR_VERSION
    prompt_version = PROMPT_VERSION
    schema_version = SCHEMA_VERSION
    model_params: ClassVar[dict] = {"strategy": "rules", "version": DETECTOR_VERSION}

    def detect(self, request: DetectorRequest) -> DetectorResult:
        messages = list(request.messages)
        candidates: list[Candidate] = []
        for index, anchor in enumerate(messages):
            if anchor.role != "window" or not _is_request(anchor):
                continue
            end = next(
                (
                    position
                    for position in range(index + 1, len(messages))
                    if _is_request(messages[position])
                ),
                len(messages),
            )
            segment = messages[index + 1 : end]
            responses = [
                item
                for item in segment
                if item.side == "vedita" and item.source_type == "message"
            ]
            unknown_after = any(item.side == "unknown" for item in segment)
            escalation = (
                next(
                    (
                        item
                        for item in segment
                        if item.side == "customer"
                        and item.source_type == "message"
                        and _ESCALATION.search(_normal(item.text))
                    ),
                    None,
                )
                if _BLOCKED_OPERATION.search(_normal(anchor.text))
                else None
            )
            candidate = _candidate(anchor, responses, unknown_after, escalation)
            if candidate is not None:
                candidates.append(candidate)

        input_size = sum(len(item.text) for item in messages)
        output_size = sum(len(candidate.description) for candidate in candidates)
        return DetectorResult(
            schema_version=self.schema_version,
            finding_type="unanswered",
            candidates=candidates,
            usage=Usage(
                input_tokens=max(0, input_size // 4),
                output_tokens=max(0, output_size // 4),
                total_tokens=max(0, (input_size + output_size) // 4),
                cost_usd=None,
                currency="USD",
                pricing_version=None,
            ),
            diagnostics=["local_baseline", f"messages={len(messages)}"],
        )
