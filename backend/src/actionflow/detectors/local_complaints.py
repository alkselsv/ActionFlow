"""Deterministic local baseline for customer complaints."""

from dataclasses import dataclass, field
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

MODEL_ID = "local-baseline-complaint-v1"
DETECTOR_VERSION = "local-complaint-v1"
PROMPT_VERSION = "complaint-rules-v1"
SCHEMA_VERSION = "detector_result.v1"

_RISK_MARKERS = (
    "останов",
    "пересмотр",
    "потребуем",
    "критич",
    "срыв",
    "простой",
)
_PROBLEM_MARKERS = (
    "не работает",
    "не передаёт",
    "не передает",
    "сбрасыв",
    "ошиб",
    "сбой",
    "проблем",
    "не получили",
    "задерж",
)
_QUOTE_MARKERS = (
    "цитат",
    "по словам",
    "как сказал",
    "как сказала",
)


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _topic(text: str) -> str:
    normalized = _normal(text)
    if _has_any(normalized, ("фильтр", "сбрасыв")):
        return "filter"
    if _has_any(normalized, ("интеграц", "заказ", "обмен")):
        return "integration"
    if _has_any(normalized, ("качество", "брак")):
        return "quality"
    return "general"


def _is_customer_message(message: InputMessage) -> bool:
    return (
        message.side in ("customer", "unknown")
        and message.source_type == "message"
        and bool(message.text.strip())
    )


def _is_quote(text: str) -> bool:
    return _has_any(_normal(text), _QUOTE_MARKERS)


def _is_complaint_seed(text: str) -> bool:
    normalized = _normal(text)
    if _is_quote(normalized):
        return False
    if _has_any(normalized, ("не устраивает", "жалоб", "претенз", "эскалац")):
        return True
    if _has_any(normalized, _RISK_MARKERS):
        return True
    if _has_any(normalized, ("снова", "опять", "третий раз")):
        return _has_any(normalized, _PROBLEM_MARKERS)
    return False


def _risk_in_conversation(messages: list[InputMessage]) -> bool:
    return any(
        message.source_type == "message"
        and _has_any(_normal(message.text), _RISK_MARKERS)
        for message in messages
    )


def _snippet(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _quote(text: str) -> str | None:
    return text if len(text) <= 2000 else text[:2000]


@dataclass
class _Episode:
    anchor: InputMessage
    topic: str
    supporting: list[InputMessage] = field(default_factory=list)
    explicit_risk: bool = False


def _key(anchor: InputMessage) -> str:
    material = f"complaint|{anchor.message_id}"
    return "complaint:" + sha256(material.encode()).hexdigest()[:40]


def _candidate(episode: _Episode) -> Candidate:
    evidence = [
        Evidence(
            role="anchor",
            message_id=episode.anchor.message_id,
            revision_id=episode.anchor.revision_id,
            quote=_quote(episode.anchor.text),
        )
    ]
    for message in episode.supporting[-5:]:
        evidence.append(
            Evidence(
                role="supporting",
                message_id=message.message_id,
                revision_id=message.revision_id,
                quote=_quote(message.text),
            )
        )

    limitations: list[str] = []
    if episode.anchor.side == "unknown":
        limitations.append("unknown_side")
    if any(message.side == "unknown" for message in episode.supporting):
        limitations.append("unknown_side")
    if episode.explicit_risk:
        limitations.append("history_incomplete")

    description = (
        f"Клиент выражает претензию или сообщает о повторяющейся проблеме: "
        f"«{_snippet(episode.anchor.text)}»."
    )
    if episode.supporting:
        description += f" Позже: «{_snippet(episode.supporting[-1].text)}»."
    return Candidate(
        candidate_key=_key(episode.anchor),
        subject=Subject(
            kind="complaint_episode",
            anchor_message_id=episode.anchor.message_id,
            anchor_revision_id=episode.anchor.revision_id,
            fragment=_snippet(episode.anchor.text),
            discriminator=f"complaint:{episode.topic}",
        ),
        description=description,
        confidence=0.78,
        priority_hint="unknown",
        suggested_action="review",
        evidence=evidence,
        explicit_risk=episode.explicit_risk,
        limitations=limitations,
    )


class LocalComplaintDetector:
    provider = "local"
    model_id = MODEL_ID
    detector_version = DETECTOR_VERSION
    prompt_version = PROMPT_VERSION
    schema_version = SCHEMA_VERSION
    model_params: ClassVar[dict] = {"strategy": "rules", "version": DETECTOR_VERSION}

    def detect(self, request: DetectorRequest) -> DetectorResult:
        messages = list(request.messages)
        episode: _Episode | None = None
        conversation_risk = _risk_in_conversation(messages)

        for message in messages:
            if not _is_customer_message(message):
                continue
            if episode is None:
                if _is_complaint_seed(message.text):
                    episode = _Episode(
                        anchor=message,
                        topic=_topic(message.text),
                        explicit_risk=conversation_risk,
                    )
                continue
            if _is_complaint_seed(message.text) or _has_any(
                _normal(message.text), _PROBLEM_MARKERS
            ):
                episode.supporting.append(message)

        candidates = [_candidate(episode)] if episode is not None else []
        input_size = sum(len(item.text) for item in messages)
        output_size = sum(len(item.description) for item in candidates)
        return DetectorResult(
            schema_version=self.schema_version,
            finding_type="complaint",
            candidates=candidates,
            usage=Usage(
                input_tokens=max(0, input_size // 4),
                output_tokens=max(0, output_size // 4),
                total_tokens=max(0, (input_size + output_size) // 4),
                cost_usd=None,
                currency="USD",
                pricing_version=None,
            ),
            diagnostics=["local_complaint_baseline", f"messages={len(messages)}"],
        )
