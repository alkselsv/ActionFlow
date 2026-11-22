"""Commitment due-date rules from AF-002, kept separate from detectors."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_NUMBER_WORDS = {
    "один": 1,
    "одну": 1,
    "одного": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "три": 3,
    "трех": 3,
    "трёх": 3,
    "четыре": 4,
    "четырех": 4,
    "четырёх": 4,
    "пять": 5,
    "пяти": 5,
}
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_DAY_RE = r"(сегодня|завтра|послезавтра)"
_DATE_RE = r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?"
_TIME_RE = r"(?:в|до)\s+(\d{1,2}):(\d{2})"


@dataclass(frozen=True)
class DueResult:
    due_at: datetime | None
    uncertain: bool
    expression: str | None
    reason: str


def _local(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone)


def _is_workday(day: date, work_hours: dict, closed: set[str]) -> bool:
    weekdays = {int(item) for item in work_hours.get("weekdays", [])}
    return (
        day.isoweekday() in weekdays
        and day.isoformat() not in closed
        and work_hours.get("start") != work_hours.get("end")
    )


def _at(local_day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    return datetime.combine(local_day, time(hour, minute), tzinfo=zone).astimezone(
        UTC
    )


def _time_from_match(match: re.Match | None) -> tuple[int, int] | None:
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_due(
    text: str,
    sent_at: datetime,
    *,
    timezone: str,
    work_hours: dict,
    closed_dates: tuple[str, ...] = (),
) -> DueResult:
    """Parse the agreed AF-002 commitment deadline expressions."""
    zone = ZoneInfo(timezone)
    local = _local(sent_at, zone)
    normalized = " ".join(text.casefold().split())
    closed = set(closed_dates)
    time_match = re.search(_TIME_RE, normalized)

    duration = re.search(
        r"(?:в течение|через)\s+(\d+|[а-яё]+)\s+(час(?:а|ов)?|минут(?:а|ы)?)",
        normalized,
    )
    if duration:
        raw = duration.group(1)
        amount = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
        if amount is None:
            return DueResult(
                None, True, duration.group(0), "unsupported_duration"
            )
        unit = duration.group(2)
        delta = (
            timedelta(hours=amount)
            if unit.startswith("час")
            else timedelta(minutes=amount)
        )
        return DueResult(
            (local + delta).astimezone(UTC),
            False,
            duration.group(0),
            "calendar_duration",
        )

    explicit_date = re.search(
        rf"(?:до|к|в)\s+{_DATE_RE}(?:\s*(?:в|до)?\s+(\d{{1,2}}):(\d{{2}}))?",
        normalized,
    )
    if explicit_date:
        day = int(explicit_date.group(1))
        month = _MONTHS[explicit_date.group(2)]
        year = int(explicit_date.group(3) or local.year)
        hour, minute = (
            int(explicit_date.group(4)),
            int(explicit_date.group(5)),
        ) if explicit_date.group(4) else (18, 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return DueResult(None, True, explicit_date.group(0), "invalid_time")
        target = date(year, month, day)
        if target < local.date():
            target = date(year + 1, month, day)
        return DueResult(
            _at(target, hour, minute, zone),
            False,
            explicit_date.group(0),
            "explicit_date",
        )

    relative = re.search(_DAY_RE, normalized)
    if relative:
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[
            relative.group(1)
        ]
        target = local.date() + timedelta(days=offset)
        parsed_time = _time_from_match(time_match)
        if parsed_time is None:
            if not _is_workday(target, work_hours, closed):
                return DueResult(
                    None, True, relative.group(0), "nonworking_day_without_time"
                )
            parsed_time = (18, 0)
        return DueResult(
            _at(target, parsed_time[0], parsed_time[1], zone),
            False,
            relative.group(0),
            "relative_day",
        )

    if time_match:
        parsed_time = _time_from_match(time_match)
        if parsed_time is None:
            return DueResult(None, True, time_match.group(0), "invalid_time")
        return DueResult(
            _at(local.date(), parsed_time[0], parsed_time[1], zone),
            False,
            time_match.group(0),
            "today_time",
        )

    return DueResult(None, False, None, "no_deadline")
