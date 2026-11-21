from datetime import UTC, date, time
from re import fullmatch
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select

from actionflow.persistence.database import Project
from actionflow.persistence.project_settings import (
    Chat,
    Participant,
    ParticipantSide,
    SettingsVersion,
)

AuthorId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class WorkHours(BaseModel):
    weekdays: list[Annotated[int, Field(strict=True, ge=1, le=7)]] = Field(
        min_length=1, max_length=7
    )
    start: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    end: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]

    @model_validator(mode="after")
    def validate_window(self):
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Рабочие дни не должны повторяться")
        if time.fromisoformat(self.start) >= time.fromisoformat(self.end):
            raise ValueError("Конец рабочего дня должен быть позже начала")
        self.weekdays.sort()
        return self


class Calendar(BaseModel):
    closed_dates: list[date] = Field(default_factory=list, max_length=366)

    @field_validator("closed_dates", mode="before")
    @classmethod
    def date_formats(cls, values):
        if isinstance(values, list) and any(
            not isinstance(value, str)
            or not fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            for value in values
        ):
            raise ValueError("Используйте даты в формате ГГГГ-ММ-ДД")
        return values

    @field_validator("closed_dates")
    @classmethod
    def unique_dates(cls, dates):
        if len(set(dates)) != len(dates):
            raise ValueError("Даты исключений не должны повторяться")
        return sorted(dates)


class SideInput(BaseModel):
    source_author_id: AuthorId
    side: Literal["vedita", "customer", "unknown"]


class SettingsInput(BaseModel):
    base_version_id: str | None = Field(default=None, max_length=36, exclude=True)
    context_text: str = Field(max_length=20_000)
    agreements: str = Field(default="", max_length=20_000)
    manager_name: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
        ]
        | None
    ) = None
    client_type: Literal["north", "south", "custom"] = "custom"
    work_hours: WorkHours
    timezone: str = Field(min_length=1, max_length=100)
    first_response_minutes: int = Field(strict=True, ge=1, le=525_600)
    calendar: Calendar
    participant_sides: list[SideInput] = Field(max_length=1000)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(
                "Укажите часовой пояс IANA, например Europe/Moscow"
            ) from None
        return value

    @field_validator("participant_sides")
    @classmethod
    def distinct_authors(cls, sides):
        if len({side.source_author_id for side in sides}) != len(sides):
            raise ValueError("Идентификаторы участников не должны повторяться")
        return sides


def require_project(db, project_id, *, lock=False):
    query = select(Project).where(Project.id == project_id)
    if lock:
        query = query.with_for_update()
    project = db.scalar(query)
    if project is None:
        raise HTTPException(404, "Проект не найден")
    return project


def settings_versions(project_id):
    return (
        select(SettingsVersion)
        .where(SettingsVersion.project_id == project_id)
        .order_by(SettingsVersion.version_no.desc())
    )


def current_settings(db, project_id):
    return db.scalar(settings_versions(project_id).limit(1))


def settings_result(version):
    created = version.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "id": version.id,
        "version_no": version.version_no,
        "context_text": version.context_text,
        "agreements": version.agreements,
        "manager_name": version.manager_name,
        "client_type": version.client_type,
        "work_hours": version.work_hours,
        "timezone": version.timezone,
        "first_response_minutes": version.first_response_minutes,
        "calendar": version.calendar,
        "participant_sides": [
            {"source_author_id": side.source_author_id, "side": side.side}
            for side in sorted(version.sides, key=lambda row: row.source_author_id)
        ],
        "created_at": created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def participants_result(db, project_id, version):
    known = (
        {side.source_author_id: side.side for side in version.sides} if version else {}
    )
    imported = dict(
        db.execute(
            select(Participant.source_author_id, Participant.display_name)
            .join(Chat)
            .where(Chat.project_id == project_id)
        ).all()
    )
    return {
        "settings_version_id": version.id if version else None,
        "items": [
            {
                "source_author_id": author_id,
                "display_name": imported.get(author_id),
                "imported": author_id in imported,
                "side": known.get(author_id, "unknown"),
            }
            for author_id in sorted(imported.keys() | known.keys())
        ],
    }


def save_settings(db, project, data):
    previous = current_settings(db, project.id)
    if "base_version_id" in data.model_fields_set and data.base_version_id != (
        previous.id if previous else None
    ):
        raise HTTPException(409, "Настройки уже изменились. Загрузите последнюю версию")
    if data.manager_name is not None:
        project.manager_name = data.manager_name
    incoming = data.model_dump(
        mode="json", exclude={"participant_sides", "manager_name"}
    )
    version = SettingsVersion(
        project_id=project.id,
        version_no=previous.version_no + 1 if previous else 1,
        manager_name=project.manager_name,
        **incoming,
        sides=[ParticipantSide(**side.model_dump()) for side in data.participant_sides],
    )
    db.add(version)
    db.commit()
    return settings_result(version)
