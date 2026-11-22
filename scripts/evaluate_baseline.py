"""Run AF-002 slices through the local detectors in a disposable database."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from actionflow.api.telegram_import import import_chat
from actionflow.application.run_results import FINDING_TYPES, detector_results
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import (
    Base,
    Client,
    Project,
    as_utc,
    database,
    new_id,
)
from actionflow.persistence.findings import Candidate, FindingObservation
from actionflow.persistence.project_settings import (
    Chat,
    Message,
    ParticipantSide,
    SettingsVersion,
)
from actionflow.temporal.activities import AnalysisActivities
from sqlalchemy import select

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/telegram"
# Expected type, anchor, acceptable evidence IDs, and optional first-response SLA.
# IDs refer to the local Telegram message numbers in the AF-002 annotation.
SLICES = (
    (
        "development",
        "n-001",
        "14T06:30",
        (
            ("unanswered", 2, (2, 3), "met", "14T07:07", "14T06:25"),
            ("commitment", 3, (3,)),
        ),
    ),
    ("development", "n-001", "14T09:15", (("commitment", 6, (6,)),)),
    (
        "development",
        "n-001",
        "15T10:35",
        (
            ("commitment", 6, (6, 10)),
            ("unanswered", 12, (12,), "breached", "15T10:30", None),
        ),
    ),
    (
        "development",
        "n-002",
        "15T10:05",
        (
            ("complaint", 1, (1, 2, 3)),
            ("unanswered", 3, (3, 4), "met", "15T07:13", "15T06:35"),
            ("commitment", 4, (4, 6)),
        ),
    ),
    ("development", "n-004", "15T08:30", ()),
    (
        "development",
        "n-005",
        "15T09:25",
        (("unanswered", 1, (1, 4), "breached", "15T09:00", "15T09:20"),),
    ),
    ("development", "n-008", "15T08:00", (("commitment", 2, (2,)),)),
    (
        "holdout",
        "n-003",
        "15T11:00",
        (
            ("unanswered", 1, (1, 2), "met", "15T11:00", "15T10:05"),
            ("commitment", 2, (2,)),
        ),
    ),
    ("holdout", "n-006", "15T11:25", ()),
    (
        "holdout",
        "n-006",
        "15T12:35",
        (("unanswered", 2, (2,), "breached", "15T12:30", None),),
    ),
    (
        "holdout",
        "n-007",
        "15T08:35",
        (
            ("complaint", 1, (1, 3)),
            ("unanswered", 1, (1, 2, 3), "met", "15T07:00", "15T06:05"),
            ("commitment", 2, (2,)),
        ),
    ),
    ("holdout", "s-001", "15T06:30", (("commitment", 2, (2, 3)),)),
)


def timestamp(value):
    return datetime.fromisoformat(f"2026-09-{value}:00+00:00") if value else None


def prepare_slice(sessions, code, cutoff):
    south = code.startswith("s")
    payload = (
        FIXTURES / f"{'south' if south else 'north'}-conv-{code}.json"
    ).read_bytes()
    records = json.loads(payload)["messages"]
    external_ids = {
        record["_actionflow_synthetic_source"]["external_id"]: record["id"]
        for record in records
    }
    sides = {
        record["from_id"]: "customer" if role == "customer" else "vedita"
        for record in records
        if (role := record["_actionflow_synthetic_source"]["sender_role"])
        in {"customer", "employee"}
    }
    with sessions.begin() as db:
        project = Project(
            client=Client(name="Synthetic"), name=code, manager_name="Manager"
        )
        db.add(project)
        db.flush()
        project_id = project.id
    with sessions() as db:
        project = db.get(Project, project_id)
        import_chat(db, project, payload)
    with sessions.begin() as db:
        chat = db.scalar(select(Chat).where(Chat.project_id == project_id))
        settings = SettingsVersion(
            project_id=project_id,
            version_no=1,
            context_text="",
            agreements="",
            manager_name="Manager",
            client_type="south" if south else "north",
            work_hours={"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"},
            calendar={"closed_dates": []},
            timezone="Europe/Moscow",
            first_response_minutes=120 if south else 60,
            created_at=timestamp("14T00:00"),
            sides=[
                ParticipantSide(source_author_id=author, side=side)
                for author, side in sides.items()
            ],
        )
        db.add(settings)
        db.flush()
        run_id = new_id()
        db.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                chat_id=chat.id,
                settings_version_id=settings.id,
                period_start=timestamp("14T00:00"),
                period_end=cutoff,
                as_of=cutoff,
                apply_to_queue=True,
                request_hash="evaluation",
                workflow_id=f"evaluation:{run_id}",
            )
        )
        return run_id, external_ids


def score_slice(sessions, run_id, external_ids, expected):
    remaining = {
        (kind, anchor): (evidence, sla) for kind, anchor, evidence, *sla in expected
    }
    totals = {kind: Counter() for kind in FINDING_TYPES}
    with sessions() as db:
        run = db.get(AnalysisRun, run_id)
        ids = {
            message.id: external_ids[message.external_id]
            for message in db.scalars(
                select(Message).where(Message.chat_id == run.chat_id)
            )
        }
        seen = set()
        rows = db.execute(
            select(Candidate, FindingObservation)
            .outerjoin(
                FindingObservation, FindingObservation.candidate_id == Candidate.id
            )
            .where(Candidate.run_id == run_id)
            .order_by(Candidate.candidate_key)
        )
        for candidate, observation in rows:
            metrics = totals[candidate.finding_type]
            if candidate.validation_status != "valid":
                metrics["invalid"] += 1
                continue
            if observation is not None and observation.commitment_status in {
                "fulfilled",
                "cancelled",
            }:
                continue
            key = (candidate.finding_type, ids[candidate.anchor_message_id])
            metrics["duplicates"] += int(key in seen)
            seen.add(key)
            match = remaining.pop(key, None)
            if match is None:
                metrics["fp"] += 1
                continue
            metrics["tp"] += 1
            acceptable, sla = match
            used = {
                ids[item["message_id"]] for item in candidate.raw_payload["evidence"]
            }
            required = {key[1]}
            if sla and sla[-1] is not None:
                required.add(acceptable[1])
            if (key == ("commitment", 4) and acceptable == (4, 6)) or (
                key == ("complaint", 1) and acceptable == (1, 3)
            ):
                required.update(acceptable)
            metrics["evidence_correct"] += int(required <= used <= set(acceptable))
            if sla:
                metrics["sla_checked"] += 1
                state, due, first_response = sla
                metrics["sla_errors"] += int(
                    observation is None
                    or (
                        observation.sla_state,
                        as_utc(observation.sla_due_at),
                        as_utc(observation.first_response_at),
                    )
                    != (state, timestamp(due), timestamp(first_response))
                )
        for kind, _ in remaining:
            totals[kind]["fn"] += 1
        results = detector_results(run)
        limitations = dict(run.limitations or {})
        for key in FINDING_TYPES:
            limitations.pop(key, None)
        return totals, {
            "duration_seconds": (
                as_utc(run.finished_at) - as_utc(run.started_at)
            ).total_seconds(),
            "cost_usd": float(run.cost_usd) if run.cost_usd is not None else None,
            "input_limitations": limitations,
            "detectors": {
                kind: {
                    field: detail.get(field)
                    for field in ("model", "detector_version", "duration_ms", "usage")
                }
                for kind, detail in results.items()
            },
        }


def main():
    partitions = {
        name: {kind: Counter() for kind in FINDING_TYPES}
        for name in ("development", "holdout")
    }
    details = []
    for split, code, cutoff, expected in SLICES:
        engine, sessions = database("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            run_id, external_ids = prepare_slice(sessions, code, timestamp(cutoff))
            activities = AnalysisActivities(sessions)
            activities.prepare(run_id)
            activities.detect(run_id)
            activities.detect_commitment(run_id)
            activities.detect_complaint(run_id)
            activities.finalize(run_id)
            totals, detail = score_slice(sessions, run_id, external_ids, expected)
        finally:
            engine.dispose()
        for kind, counts in totals.items():
            partitions[split][kind].update(counts)
        details.append(
            {
                "split": split,
                "chat": code,
                "as_of": timestamp(cutoff).isoformat(),
                **detail,
                "counts": totals,
            }
        )
    for partition in partitions.values():
        for counts in partition.values():
            for key in (
                "tp",
                "fp",
                "fn",
                "duplicates",
                "evidence_correct",
                "sla_checked",
                "sla_errors",
                "invalid",
            ):
                counts.setdefault(key, 0)
            tp = counts["tp"]
            counts["precision"] = (
                tp / (tp + counts["fp"]) if tp + counts["fp"] else None
            )
            counts["recall"] = tp / (tp + counts["fn"]) if tp + counts["fn"] else None
    print(json.dumps({"metrics": partitions, "slices": details}, indent=2))


if __name__ == "__main__":
    main()
