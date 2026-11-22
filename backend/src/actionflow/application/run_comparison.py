from collections import defaultdict
from json import dumps

from sqlalchemy import select

from actionflow.application.run_results import detector_results
from actionflow.persistence.analysis import AnalysisInputMessage
from actionflow.persistence.database import as_utc
from actionflow.persistence.findings import Candidate, FindingObservation


def input_messages(db, run):
    rows = db.scalars(
        select(AnalysisInputMessage)
        .where(AnalysisInputMessage.run_id == run.id)
        .order_by(AnalysisInputMessage.message_id)
    )
    return [
        (
            row.message_id,
            row.revision_id,
            row.role,
            row.participant_side,
            row.observed_at,
            row.sent_at,
            row.source_observed_at,
        )
        for row in rows
    ]


def same_slice(db, first, second):
    return (
        first.input_hash is not None
        and second.input_hash is not None
        and first.chat_id == second.chat_id
        and first.period_start == second.period_start
        and first.period_end == second.period_end
        and first.as_of == second.as_of
        and input_messages(db, first) == input_messages(db, second)
    )


def candidate_groups(db, run, kinds, include_observations):
    groups = defaultdict(list)
    rows = db.execute(
        select(Candidate, FindingObservation)
        .outerjoin(FindingObservation, FindingObservation.candidate_id == Candidate.id)
        .where(Candidate.run_id == run.id, Candidate.finding_type.in_(kinds))
        .order_by(Candidate.candidate_key)
    )
    for candidate, observation in rows:
        subject_id = (
            candidate.anchor_message_id
            if candidate.finding_type == "commitment"
            else candidate.subject_id
        )
        key = (candidate.finding_type, candidate.subject_kind, subject_id)
        groups[key].append(
            {
                "description": candidate.description,
                "validity": candidate.validation_status,
                "errors": candidate.validation_errors,
                "evidence": sorted(
                    candidate.raw_payload.get("evidence", []),
                    key=lambda item: (
                        item["role"],
                        item["message_id"],
                        item["revision_id"],
                        item.get("quote") or "",
                    ),
                ),
                "sla_state": observation.sla_state
                if include_observations and observation
                else None,
                "sla_due_at": (
                    as_utc(observation.sla_due_at).isoformat()
                    if include_observations and observation and observation.sla_due_at
                    else None
                ),
                "commitment": candidate.raw_payload.get("commitment"),
            }
        )
    return {
        key: sorted(items, key=lambda item: dumps(item, sort_keys=True))
        for key, items in groups.items()
    }


def compare_runs(db, first, second):
    first_results, second_results = detector_results(first), detector_results(second)
    kinds = [
        kind
        for kind in first_results
        if first_results[kind]["status"]
        == second_results[kind]["status"]
        == "succeeded"
    ]
    include_observations = first.apply_to_queue and second.apply_to_queue
    before = candidate_groups(db, first, kinds, include_observations)
    after = candidate_groups(db, second, kinds, include_observations)
    changes = []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key, []), after.get(key, [])
        if left != right:
            kind, subject_kind, subject_id = key
            changes.append(
                {
                    "type": kind,
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "before": left,
                    "after": right,
                }
            )
    return changes
