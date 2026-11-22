from __future__ import annotations

import socket
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select, text

from actionflow.config import Settings
from actionflow.persistence.analysis import AnalysisRun, OutboxEvent
from actionflow.persistence.database import (
    WorkerHeartbeat,
    as_utc,
    utc_now,
)

HEARTBEAT_STALE_SECONDS = 30


def record_worker_heartbeat(sessions, task_queue: str, name: str = "worker") -> None:
    """Record a heartbeat without touching conversation content."""
    now = utc_now()
    with sessions.begin() as db:
        heartbeat = db.get(WorkerHeartbeat, name)
        if heartbeat is None:
            db.add(
                WorkerHeartbeat(
                    name=name,
                    task_queue=task_queue,
                    host=socket.gethostname()[:255],
                    started_at=now,
                    last_seen_at=now,
                )
            )
        else:
            heartbeat.task_queue = task_queue
            heartbeat.host = socket.gethostname()[:255]
            heartbeat.last_seen_at = now


def heartbeat_status(
    db,
    name: str = "worker",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    heartbeat = db.get(WorkerHeartbeat, name)
    if heartbeat is None:
        return {
            "name": name,
            "available": False,
            "reason": "not_seen",
            "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
        }
    last_seen = as_utc(heartbeat.last_seen_at)
    current = as_utc(now or utc_now())
    age = max(0.0, (current - last_seen).total_seconds())
    available = age <= HEARTBEAT_STALE_SECONDS
    return {
        "name": name,
        "available": available,
        "reason": "ok" if available else "stale",
        "last_seen_at": last_seen,
        "started_at": as_utc(heartbeat.started_at),
        "age_seconds": round(age, 1),
        "stale_after_seconds": HEARTBEAT_STALE_SECONDS,
        "task_queue": heartbeat.task_queue,
        "host": heartbeat.host,
    }


def provider_status(
    settings: Settings,
    *,
    probe: bool = False,
) -> dict[str, Any]:
    configured = settings.llm_configured
    result: dict[str, Any] = {
        "mode": "external" if configured else "local_baseline",
        "available": True if not configured else None,
        "checked": False,
        "provider": settings.llm_provider if configured else "local-baseline",
        "model": settings.llm_model if configured else "local-baseline",
        "base_url": settings.llm_base_url if configured else None,
        "key_present": bool(settings.llm_api_key),
        "timeout_seconds": settings.llm_timeout_seconds,
        "max_attempts": settings.llm_max_attempts,
        "budget_configured": settings.llm_budget_usd is not None,
        "note": (
            "External provider is configured. Availability is checked without "
            "sending conversation text."
            if configured
            else "Local deterministic baseline is active; no external API is called."
        ),
    }
    if not configured or not probe:
        return result

    base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
    request = Request(
        f"{base_url}/models",
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    result["checked"] = True
    try:
        with urlopen(request, timeout=min(settings.llm_timeout_seconds, 10)):
            result["available"] = True
            result["reason"] = "ok"
    except HTTPError as failure:
        result["available"] = False
        result["reason"] = f"http_{failure.code}"
    except (URLError, TimeoutError, OSError) as failure:
        result["available"] = False
        result["reason"] = type(failure).__name__
    return result


def _run_result(run: AnalysisRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "status": run.status,
        "stage": run.stage,
        "error_code": run.error_code,
        "retryable": run.retryable,
        "workflow_id": run.workflow_id,
        "temporal_run_id": run.temporal_run_id,
        "as_of": as_utc(run.as_of),
        "input_hash": run.input_hash,
        "provider": run.provider,
        "model_id": run.model_id,
        "created_at": as_utc(run.created_at),
        "started_at": as_utc(run.started_at),
        "finished_at": as_utc(run.finished_at),
    }


def _outbox_result(event: OutboxEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "kind": event.kind,
        "status": event.status,
        "attempts": event.attempts,
        "next_attempt_at": as_utc(event.next_attempt_at),
        "dispatched_at": as_utc(event.dispatched_at),
        "last_error": event.last_error,
    }


def run_diagnostics(
    db,
    run_id: str,
    settings: Settings,
    *,
    probe_provider: bool = False,
) -> dict[str, Any] | None:
    run = db.get(AnalysisRun, run_id)
    if run is None:
        return None
    outbox = list(
        db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.aggregate_id == run.id)
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
        )
    )
    return {
        "run": _run_result(run),
        "outbox": [_outbox_result(event) for event in outbox],
        "worker": heartbeat_status(db),
        "provider": provider_status(settings, probe=probe_provider),
        "logging": {
            "content_policy": "Message text and secrets are excluded from operational logs.",
            "identifiers": ["run_id", "workflow_id", "stage", "error_code"],
        },
    }


def service_diagnostics(
    db,
    settings: Settings,
    *,
    probe_provider: bool = False,
) -> dict[str, Any]:
    db.execute(text("SELECT 1"))
    return {
        "database": {"available": True},
        "worker": heartbeat_status(db),
        "provider": provider_status(settings, probe=probe_provider),
        "logging": {
            "content_policy": "Message text and secrets are excluded from operational logs.",
            "identifiers": ["run_id", "workflow_id", "stage", "error_code"],
        },
    }
