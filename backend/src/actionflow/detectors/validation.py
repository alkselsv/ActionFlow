"""Validate detector output against the immutable input of one analysis run."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from actionflow.detectors.contracts import Candidate, DetectorResult, InputMessage

ValidationStatus = Literal["valid", "invalid"]


@dataclass(frozen=True)
class CandidateValidation:
    candidate: Candidate
    status: ValidationStatus
    errors: tuple[str, ...]
    subject_id: str


def normalized(value: str) -> str:
    return " ".join(value.split())


def quote_hash(value: str | None) -> str | None:
    return sha256(normalized(value).encode()).hexdigest() if value is not None else None


def validate_result(
    result: DetectorResult,
    messages: tuple[InputMessage, ...],
    *,
    allowed_anchor_sides: tuple[str, ...] = ("customer", "unknown"),
    expected_subject_kind: str = "message",
) -> tuple[CandidateValidation, ...]:
    by_message = {message.message_id: message for message in messages}
    validations: list[CandidateValidation] = []
    for candidate in result.candidates:
        errors: list[str] = []
        subject = candidate.subject
        if subject.kind != expected_subject_kind:
            errors.append(f"unexpected_subject_kind:{subject.kind}")
        anchor = by_message.get(subject.anchor_message_id)
        if anchor is None:
            errors.append(f"anchor_not_in_input:{subject.anchor_message_id}")
        else:
            if subject.anchor_revision_id != anchor.revision_id:
                errors.append("anchor_revision_mismatch")
            if anchor.role != "window":
                errors.append("anchor_outside_window")
            if anchor.side not in allowed_anchor_sides:
                errors.append(f"anchor_side_not_customer:{anchor.side}")

        anchors = [item for item in candidate.evidence if item.role == "anchor"]
        if len(anchors) != 1:
            errors.append("exactly_one_anchor_evidence_required")
        seen: set[tuple[str, str, str, str | None]] = set()
        for evidence in candidate.evidence:
            message = by_message.get(evidence.message_id)
            if message is None:
                errors.append(f"evidence_not_in_input:{evidence.message_id}")
                continue
            if evidence.revision_id != message.revision_id:
                errors.append(f"evidence_revision_mismatch:{evidence.message_id}")
            if evidence.role == "anchor" and (
                evidence.message_id != subject.anchor_message_id
                or evidence.revision_id != subject.anchor_revision_id
            ):
                errors.append("anchor_evidence_does_not_match_subject")
            if evidence.quote is not None and normalized(evidence.quote) not in normalized(
                message.text
            ):
                errors.append(f"quote_not_in_revision:{evidence.message_id}")
            if evidence.quote is None and message.text.strip():
                errors.append(f"quote_required_for_text:{evidence.message_id}")
            if evidence.quote is not None and not message.text.strip():
                errors.append(f"quote_not_available_for_empty_text:{evidence.message_id}")
            key = (
                evidence.role,
                evidence.message_id,
                evidence.revision_id,
                quote_hash(evidence.quote),
            )
            if key in seen:
                errors.append(f"duplicate_evidence:{evidence.message_id}")
            seen.add(key)

        validations.append(
            CandidateValidation(
                candidate=candidate,
                status="invalid" if errors else "valid",
                errors=tuple(errors),
                subject_id=subject.anchor_message_id,
            )
        )
    return tuple(validations)
