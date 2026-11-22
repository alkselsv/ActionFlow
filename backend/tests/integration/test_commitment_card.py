import json
import os
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from actionflow.api.app import create_app
from actionflow.application import analysis_input
from actionflow.config import Settings
from actionflow.persistence.database import database
from actionflow.persistence.project_settings import SettingsVersion
from actionflow.setup_manager import create_manager
from actionflow.temporal.activities import AnalysisActivities

ORIGIN = {"Origin": "http://127.0.0.1:5173"}
FIXTURES = Path(__file__).resolve().parents[3] / "data/fixtures/telegram"
PASSWORD = "synthetic-test-password"


def setup_db(tmp_path, monkeypatch):
    settings = Settings(
        os.environ.get("ACTIONFLOW_CARD_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'commitments.db'}"
    )
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    config = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    command.upgrade(config, "head")
    _, sessions = database(settings.database_url)
    create_manager(sessions, "manager", PASSWORD)
    return settings, sessions


def setup_project(
    browser, fixture="north-conv-n-001.json", payload: bytes | None = None
):
    login = browser.post(
        "/api/v1/auth/login",
        json={"username": "manager", "password": PASSWORD},
        headers=ORIGIN,
    )
    assert login.status_code == 200
    project = browser.post(
        "/api/v1/projects",
        json={
            "client_name": "Synthetic client",
            "project_name": "Commitment project",
            "manager_name": "Manager",
        },
        headers=ORIGIN,
    )
    assert project.status_code == 201
    project_id = project.json()["id"]
    settings = browser.post(
        f"/api/v1/projects/{project_id}/settings",
        headers=ORIGIN,
        json={
            "context_text": "Synthetic context",
            "agreements": "",
            "manager_name": "Manager",
            "client_type": "north",
            "work_hours": {
                "weekdays": [1, 2, 3, 4, 5],
                "start": "09:00",
                "end": "18:00",
            },
            "timezone": "Europe/Moscow",
            "first_response_minutes": 60,
            "calendar": {"closed_dates": []},
            "participant_sides": [
                {"source_author_id": "user9001", "side": "customer"},
                {"source_author_id": "user9101", "side": "vedita"},
                {"source_author_id": "user9002", "side": "customer"},
                {"source_author_id": "user9102", "side": "vedita"},
            ],
        },
    )
    assert settings.status_code == 201
    settings_id = settings.json()["id"]
    imported = browser.post(
        f"/api/v1/projects/{project_id}/messages/import",
        headers={**ORIGIN, "Content-Type": "application/json"},
        content=payload if payload is not None else (FIXTURES / fixture).read_bytes(),
    )
    assert imported.status_code == 200
    return project_id, settings_id


def run_pipeline(
    browser, sessions, project_id, *, key, as_of, period_start="2026-09-14T00:00:00Z"
):
    response = browser.post(
        f"/api/v1/projects/{project_id}/analyses",
        headers={**ORIGIN, "Idempotency-Key": key},
        json={
            "period_start": period_start,
            "period_end": as_of,
            "as_of": as_of,
            "apply_to_queue": True,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    analysis_input.prepare_input(sessions, run_id, finalize=False)
    activities = AnalysisActivities(sessions)
    activities.detect(run_id)
    activities.detect(run_id)
    activities.detect_commitment(run_id)
    activities.detect_commitment(run_id)
    activities.finalize(run_id)
    return run_id


def commitment_cards(browser, project_id):
    response = browser.get(
        f"/api/v1/projects/{project_id}/findings?type=commitment"
    )
    assert response.status_code == 200
    return response.json()["items"]


def test_commitment_history_survives_repeated_analysis_and_window_change(
    tmp_path, monkeypatch
):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser)
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

        first = run_pipeline(
            browser,
            sessions,
            project_id,
            key="commitment-first",
            as_of="2026-09-14T06:30:00Z",
        )
        run = browser.get(f"/api/v1/analyses/{first}").json()
        assert run["status"] == "succeeded"
        assert run["stage"] == "findings_ready"
        assert run["implemented_types"] == ["unanswered", "commitment"]
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 1
        first_card = browser.get(f"/api/v1/findings/{cards[0]['id']}").json()
        assert first_card["priority"] == "medium"
        assert first_card["commitment"]["status"] == "open"
        assert first_card["commitment"]["due_at"] is None
        assert [item["kind"] for item in first_card["commitment"]["events"]] == [
            "promise"
        ]

        second = run_pipeline(
            browser,
            sessions,
            project_id,
            key="commitment-second",
            as_of="2026-09-14T09:15:00Z",
        )
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 2
        by_anchor = {
            item["anchor_message_id"]: item
            for item in cards
        }
        assert len(by_anchor) == 2
        cards_detail = [
            browser.get(f"/api/v1/findings/{item['id']}").json()
            for item in cards
        ]
        estimate = next(
            item for item in cards_detail if "смет" in item["description"]
        )
        explanation = next(
            item for item in cards_detail if item is not estimate
        )
        assert explanation["commitment"]["status"] == "fulfilled"
        assert explanation["status"] == "new"
        assert [e["kind"] for e in explanation["commitment"]["events"]] == [
            "promise",
            "fulfill",
        ]
        assert estimate["commitment"]["status"] == "open"
        assert estimate["priority"] == "low"
        assert estimate["commitment"]["due_at"].startswith(
            "2026-09-15T09:00:00"
        )

        # The first slice keeps only its own evidence even after later runs.
        old_card = browser.get(
            f"/api/v1/findings/{explanation['id']}?run_id={first}"
        ).json()
        assert [e["kind"] for e in old_card["commitment"]["events"]] == [
            "promise"
        ]

        third = run_pipeline(
            browser,
            sessions,
            project_id,
            key="commitment-third",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 2
        overdue = next(
            browser.get(f"/api/v1/findings/{item['id']}").json()
            for item in cards
            if "смет" in item["description"]
        )
        assert overdue["priority"] == "high"
        assert "commitment_deadline_missed" in overdue["priority_reason"]["reasons"]
        assert overdue["commitment"]["status"] == "open"
        assert overdue["commitment"]["events"][0]["kind"] == "promise"
        assert overdue["current_run_id"] == third
        assert second != third


def test_repeat_updates_one_commitment_without_creating_a_duplicate(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser, "north-conv-n-002.json")
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

        run_id = run_pipeline(
            browser,
            sessions,
            project_id,
            key="commitment-repeat",
            as_of="2026-09-15T10:05:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        run = browser.get(f"/api/v1/analyses/{run_id}").json()
        assert run["stage"] == "findings_ready"
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 1
        card = browser.get(f"/api/v1/findings/{cards[0]['id']}").json()
        assert card["commitment"]["status"] == "open"
        assert [event["kind"] for event in card["commitment"]["events"]] == [
            "promise",
            "repeat",
        ]
        assert card["commitment"]["due_at"].startswith("2026-09-15T15:00:00")
        assert card["priority"] == "low"
        assert card["status"] == "new"


def test_promise_reschedule_and_fulfillment_are_kept_in_one_history(
    tmp_path, monkeypatch
):
    payload = json.dumps(
        {
            "name": "Synthetic reschedule",
            "type": "private_group",
            "id": 100099,
            "messages": [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-09-15T07:00:00",
                    "date_unixtime": "1789455600",
                    "from": "Synthetic Customer",
                    "from_id": "user9002",
                    "text": "Нужна выгрузка по продажам.",
                    "_actionflow_synthetic_source": {
                        "external_id": "r-001",
                        "revision": 1,
                        "sender_role": "customer",
                        "sent_at": "2026-09-15T10:00:00+03:00",
                        "observed_at": "2026-09-15T10:01:00+03:00",
                        "reply_to_external_id": None,
                        "source": "import",
                    },
                },
                {
                    "id": 2,
                    "type": "message",
                    "date": "2026-09-15T07:05:00",
                    "date_unixtime": "1789455900",
                    "from": "Synthetic Vedita Employee",
                    "from_id": "user9102",
                    "text": "Подготовлю выгрузку сегодня до 17:30.",
                    "_actionflow_synthetic_source": {
                        "external_id": "r-002",
                        "revision": 1,
                        "sender_role": "employee",
                        "sent_at": "2026-09-15T10:05:00+03:00",
                        "observed_at": "2026-09-15T10:06:00+03:00",
                        "reply_to_external_id": "r-001",
                        "source": "import",
                    },
                },
                {
                    "id": 3,
                    "type": "message",
                    "date": "2026-09-15T08:00:00",
                    "date_unixtime": "1789459200",
                    "from": "Synthetic Vedita Employee",
                    "from_id": "user9102",
                    "text": "Перенесу выгрузку на завтра.",
                    "_actionflow_synthetic_source": {
                        "external_id": "r-003",
                        "revision": 1,
                        "sender_role": "employee",
                        "sent_at": "2026-09-15T11:00:00+03:00",
                        "observed_at": "2026-09-15T11:01:00+03:00",
                        "reply_to_external_id": "r-002",
                        "source": "import",
                    },
                },
                {
                    "id": 4,
                    "type": "message",
                    "date": "2026-09-15T09:00:00",
                    "date_unixtime": "1789462800",
                    "from": "Synthetic Vedita Employee",
                    "from_id": "user9102",
                    "text": "Готово, отправила выгрузку.",
                    "_actionflow_synthetic_source": {
                        "external_id": "r-004",
                        "revision": 1,
                        "sender_role": "employee",
                        "sent_at": "2026-09-15T12:00:00+03:00",
                        "observed_at": "2026-09-15T12:01:00+03:00",
                        "reply_to_external_id": "r-003",
                        "source": "import",
                    },
                },
            ],
        },
        ensure_ascii=False,
    ).encode()
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser, payload=payload)
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        run_id = run_pipeline(
            browser,
            sessions,
            project_id,
            key="commitment-reschedule",
            as_of="2026-09-15T11:00:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 1
        card = browser.get(f"/api/v1/findings/{cards[0]['id']}").json()
        assert card["commitment"]["status"] == "fulfilled"
        assert [event["kind"] for event in card["commitment"]["events"]] == [
            "promise",
            "reschedule",
            "fulfill",
        ]
        assert card["commitment"]["due_at"].startswith("2026-09-16T15:00:00")
        assert card["commitment"]["events"][1]["previous_due_at"].startswith(
            "2026-09-15T14:30:00"
        )
        assert card["status"] == "new"
        assert run_id
