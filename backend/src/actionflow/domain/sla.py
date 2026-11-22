"""SLA calculation for the first response of an unanswered request."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DATA_UNCERTAINTY = {
    "unknown_side",
    "missing_text",
    "possible_attachment_answer",
    "unparsed_content",
}


@dataclass(frozen=True)
class SlaPolicy:
    timezone: str
    work_hours: dict
    closed_dates: tuple[str, ...]
    first_response_minutes: int


@dataclass(frozen=True)
class SlaResult:
    due_at: datetime | None
    state: str
    first_response_at: datetime | None
    reason: dict


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _work_window(work_hours: dict) -> tuple[set[int], time, time]:
    weekdays = {int(day) for day in work_hours.get("weekdays", [])}
    start = time.fromisoformat(work_hours["start"])
    end = time.fromisoformat(work_hours["end"])
    if not weekdays or start >= end:
        raise ValueError("Invalid work hours")
    return weekdays, start, end


def _is_workday(day: date, weekdays: set[int], closed: set[str]) -> bool:
    return day.isoweekday() in weekdays and day.isoformat() not in closed


def _next_work_start(
    day: date,
    weekdays: set[int],
    closed: set[str],
    start_time: time,
    zone: ZoneInfo,
) -> datetime:
    candidate = day
    for _ in range(370):
        if _is_workday(candidate, weekdays, closed):
            return datetime.combine(candidate, start_time, tzinfo=zone)
        candidate += timedelta(days=1)
    raise ValueError("No workday found in calendar")


def add_working_minutes(
    start: datetime,
    minutes: int,
    *,
    timezone: str,
    work_hours: dict,
    closed_dates: tuple[str, ...],
) -> datetime:
    """Add working minutes, skipping nights, weekends and closed dates."""
    if minutes < 0:
        raise ValueError("SLA minutes must not be negative")
    zone = ZoneInfo(timezone)
    weekdays, start_time, end_time = _work_window(work_hours)
    closed = set(closed_dates)
    current = _as_utc(start).astimezone(zone)
    remaining = minutes

    if not _is_workday(current.date(), weekdays, closed):
        current = _next_work_start(
            current.date(), weekdays, closed, start_time, zone
        )
    elif current.time() < start_time:
        current = datetime.combine(current.date(), start_time, tzinfo=zone)
    elif current.time() >= end_time:
        current = _next_work_start(
            current.date() + timedelta(days=1),
            weekdays,
            closed,
            start_time,
            zone,
        )

    while remaining > 0:
        if not _is_workday(current.date(), weekdays, closed):
            current = _next_work_start(
                current.date() + timedelta(days=1),
                weekdays,
                closed,
                start_time,
                zone,
            )
        day_end = datetime.combine(current.date(), end_time, tzinfo=zone)
        if current.time() < start_time:
            current = datetime.combine(current.date(), start_time, tzinfo=zone)
        available = (day_end - current).total_seconds() / 60
        if remaining <= available:
            current = current + timedelta(minutes=remaining)
            break
        remaining -= available
        current = _next_work_start(
            current.date() + timedelta(days=1),
            weekdays,
            closed,
            start_time,
            zone,
        )
    return current.astimezone(UTC)


def evaluate_first_response(
    anchor_at: datetime,
    first_response_at: datetime | None,
    as_of: datetime,
    policy: SlaPolicy | None,
    limitations: tuple[str, ...] = (),
) -> SlaResult:
    if policy is None:
        return SlaResult(
            due_at=None,
            state="not_calculated",
            first_response_at=first_response_at,
            reason={
                "rule_version": "sla.v1",
                "reason": "missing_settings",
                "limitations": list(limitations),
            },
        )

    try:
        due_at = add_working_minutes(
            anchor_at,
            policy.first_response_minutes,
            timezone=policy.timezone,
            work_hours=policy.work_hours,
            closed_dates=policy.closed_dates,
        )
    except (KeyError, TypeError, ValueError):
        return SlaResult(
            due_at=None,
            state="not_calculated",
            first_response_at=first_response_at,
            reason={
                "rule_version": "sla.v1",
                "reason": "invalid_settings",
                "limitations": list(limitations),
            },
        )

    as_of = _as_utc(as_of)
    response = _as_utc(first_response_at) if first_response_at else None
    uncertain = any(item in DATA_UNCERTAINTY for item in limitations)
    if uncertain:
        state = "uncertain"
    elif response is not None:
        state = "met" if response <= due_at else "breached"
    elif as_of >= due_at:
        state = "breached"
    else:
        state = "pending"

    return SlaResult(
        due_at=due_at,
        state=state,
        first_response_at=response,
        reason={
            "rule_version": "sla.v1",
            "reason": "first_response",
            "sla_minutes": policy.first_response_minutes,
            "timezone": policy.timezone,
            "work_hours": policy.work_hours,
            "closed_dates": list(policy.closed_dates),
            "anchor_at": _as_utc(anchor_at).isoformat(),
            "due_at": due_at.isoformat(),
            "first_response_at": response.isoformat() if response else None,
            "evaluated_at": as_of.isoformat(),
            "limitations": list(limitations),
            "uncertain": uncertain,
        },
    )
