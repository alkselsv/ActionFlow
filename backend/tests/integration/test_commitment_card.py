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
            "project_name": f"Commitment project {fixture}",
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
    activities.detect_complaint(run_id)
    activities.detect_complaint(run_id)
    activities.finalize(run_id)
    return run_id


def commitment_cards(browser, project_id):
    response = browser.get(
        f"/api/v1/projects/{project_id}/findings?type=commitment"
    )
    assert response.status_code == 200
    return response.json()["items"]


def complaint_cards(browser, project_id):
    response = browser.get(
        f"/api/v1/projects/{project_id}/findings?type=complaint"
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
        assert run["implemented_types"] == [
            "unanswered",
            "commitment",
            "complaint",
        ]
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

        complaints = complaint_cards(browser, project_id)
        assert len(complaints) == 1
        complaint = browser.get(f"/api/v1/findings/{complaints[0]['id']}").json()
        assert complaint["type"] == "complaint"
        assert complaint["priority"] == "medium"
        assert complaint["status"] == "new"
        assert complaint["recommendation"] == "review"
        assert [item["role"] for item in complaint["evidence"]] == [
            "anchor",
            "supporting",
            "supporting",
        ]
        assert complaint["limitations"] == []
        assert complaint["subject"]["kind"] == "complaint_episode"
        queued = browser.get(
            f"/api/v1/projects/{project_id}/findings?type=complaint"
        ).json()["items"]
        assert [item["id"] for item in queued] == [complaints[0]["id"]]


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


def test_escalating_complaint_is_high_and_neutral_observation_is_ignored(
    tmp_path, monkeypatch
):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser, "north-conv-n-007.json")
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        run_id = run_pipeline(
            browser,
            sessions,
            project_id,
            key="complaint-risk",
            as_of="2026-09-15T08:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        run = browser.get(f"/api/v1/analyses/{run_id}").json()
        assert run["stage"] == "findings_ready"
        complaints = complaint_cards(browser, project_id)
        assert len(complaints) == 1
        complaint = browser.get(f"/api/v1/findings/{complaints[0]['id']}").json()
        assert complaint["priority"] == "high"
        assert "explicit_contract_or_operational_risk" in complaint[
            "priority_reason"
        ]["reasons"]
        assert [item["role"] for item in complaint["evidence"]] == [
            "anchor",
            "supporting",
        ]

        neutral_project, neutral_settings_id = setup_project(
            browser, "north-conv-n-008.json"
        )
        with sessions.begin() as db:
            version = db.get(SettingsVersion, neutral_settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        run_pipeline(
            browser,
            sessions,
            neutral_project,
            key="complaint-neutral",
            as_of="2026-09-15T08:00:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        assert complaint_cards(browser, neutral_project) == []


def test_four_manager_actions_preserve_history_and_replay(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser)
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        run_id = run_pipeline(
            browser,
            sessions,
            project_id,
            key="actions-run-1",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        cards = commitment_cards(browser, project_id)
        card = next(
            browser.get(f"/api/v1/findings/{item['id']}").json()
            for item in cards
            if "смет" in item["description"]
        )
        assert card["status"] == "new"
        finding_id = card["id"]

        def act(key, action, *, comment=None, expected_version=None, proposal_id=None):
            payload = {
                "action": action,
                "expected_version": expected_version or card["version"],
            }
            if comment:
                payload["comment"] = comment
            if proposal_id:
                payload["proposal_id"] = proposal_id
            if action == "snooze":
                payload["snooze_until"] = "2099-01-02T10:00:00Z"
            return browser.post(
                f"/api/v1/findings/{finding_id}/actions",
                headers={**ORIGIN, "Idempotency-Key": key},
                json=payload,
            )

        confirm = act("action-confirm", "confirm")
        assert confirm.status_code == 200
        confirmed = confirm.json()
        assert confirmed["status"] == "confirmed"
        assert confirmed["version"] == card["version"] + 1
        assert confirmed["actions"][0]["actor"] == "manager"
        assert confirmed["actions"][0]["action"] == "confirm"

        replay = act("action-confirm", "confirm")
        assert replay.status_code == 200
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert replay.json()["version"] == confirmed["version"]

        conflict = act("action-confirm", "reject", comment="Другое тело")
        assert conflict.status_code == 409

        already_confirmed = act(
            "action-confirm-again",
            "confirm",
            expected_version=confirmed["version"],
        )
        assert already_confirmed.status_code == 422
        assert len(
            browser.get(f"/api/v1/findings/{finding_id}").json()["actions"]
        ) == 1

        stale = act(
            "action-stale",
            "snooze",
            expected_version=card["version"],
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_finding"
        assert (
            stale.json()["error"]["details"]["current_version"]
            == confirmed["version"]
        )

        snoozed = act(
            "action-snooze",
            "snooze",
            expected_version=confirmed["version"],
        )
        assert snoozed.status_code == 200
        snoozed_card = snoozed.json()
        assert snoozed_card["status"] == "snoozed"
        assert snoozed_card["snooze_until"].startswith("2099-01-02T10:00:00")
        assert snoozed_card["actions"][-1]["snooze_until"].startswith(
            "2099-01-02T10:00:00"
        )

        # New grounds while snoozed produce a review proposal and keep the
        # original snooze time until the manager returns to the card.
        run_pipeline(
            browser,
            sessions,
            project_id,
            key="actions-snoozed-run",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        snoozed_after_run = browser.get(
            f"/api/v1/findings/{finding_id}"
        ).json()
        assert snoozed_after_run["status"] == "snoozed"
        assert snoozed_after_run["snooze_until"].startswith(
            "2099-01-02T10:00:00"
        )
        review = next(
            item
            for item in snoozed_after_run["proposals"]
            if item["kind"] == "review" and item["status"] == "open"
        )
        returned = act(
            "action-return",
            "confirm",
            expected_version=snoozed_after_run["version"],
            proposal_id=review["id"],
        )
        assert returned.status_code == 200
        returned_card = returned.json()
        assert returned_card["status"] == "confirmed"
        assert returned_card["snooze_until"] is None
        assert next(
            item
            for item in returned_card["proposals"]
            if item["id"] == review["id"]
        )["status"] == "accepted"

        rejected_without_reason = act(
            "action-reject-empty",
            "reject",
            expected_version=returned_card["version"],
        )
        assert rejected_without_reason.status_code == 422
        rejected = act(
            "action-reject",
            "reject",
            expected_version=returned_card["version"],
            comment="Клиент подтвердил закрытие вопроса",
        )
        assert rejected.status_code == 200
        rejected_card = rejected.json()
        assert rejected_card["status"] == "rejected"
        assert rejected_card["actions"][-1]["comment"] == (
            "Клиент подтвердил закрытие вопроса"
        )

        # A new analysis preserves the decision and proposes a reopen.
        run_pipeline(
            browser,
            sessions,
            project_id,
            key="actions-run-2",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        after_run = browser.get(f"/api/v1/findings/{finding_id}").json()
        assert after_run["status"] == "rejected"
        review = next(
            item
            for item in after_run["proposals"]
            if item["kind"] == "review" and item["status"] == "open"
        )
        reopened = act(
            "action-reopen",
            "confirm",
            expected_version=after_run["version"],
            proposal_id=review["id"],
        )
        assert reopened.status_code == 200
        reopened_card = reopened.json()
        assert reopened_card["status"] == "confirmed"
        assert next(
            item
            for item in reopened_card["proposals"]
            if item["id"] == review["id"]
        )["status"] == "accepted"

        closed = act(
            "action-close",
            "close",
            expected_version=reopened_card["version"],
            comment="Вопрос решён",
        )
        assert closed.status_code == 200
        closed_card = closed.json()
        assert closed_card["status"] == "closed"
        assert closed_card["actions"][-1]["to_status"] == "closed"

        # New grounds after closing produce a proposal; the card stays closed
        # until the manager explicitly accepts or dismisses it.
        run_pipeline(
            browser,
            sessions,
            project_id,
            key="actions-run-3",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        after_close = browser.get(f"/api/v1/findings/{finding_id}").json()
        assert after_close["status"] == "closed"
        reopen = next(
            item
            for item in after_close["proposals"]
            if item["kind"] == "reopen" and item["status"] == "open"
        )
        dismissed = act(
            "action-dismiss",
            "dismiss_proposal",
            expected_version=after_close["version"],
            proposal_id=reopen["id"],
        )
        assert dismissed.status_code == 200
        dismissed_card = dismissed.json()
        assert dismissed_card["status"] == "closed"
        assert next(
            item
            for item in dismissed_card["proposals"]
            if item["id"] == reopen["id"]
        )["status"] == "dismissed"

        counts = browser.get(
            f"/api/v1/projects/{project_id}/findings/counts"
        ).json()
        assert counts["by_status"]["closed"] == 1
        closed_queue = browser.get(
            f"/api/v1/projects/{project_id}/findings",
            params={"status": "closed"},
        ).json()
        assert [item["id"] for item in closed_queue["items"]] == [finding_id]
        assert run_id


def test_completed_commitment_proposes_but_does_not_close_card(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser, "north-conv-n-004.json")
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        run_pipeline(
            browser,
            sessions,
            project_id,
            key="completed-commitment",
            as_of="2026-09-15T08:30:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        cards = commitment_cards(browser, project_id)
        assert len(cards) == 1
        card = browser.get(f"/api/v1/findings/{cards[0]['id']}").json()
        assert card["commitment"]["status"] == "fulfilled"
        assert card["status"] == "new"
        proposal = next(
            item
            for item in card["proposals"]
            if item["kind"] == "close" and item["status"] == "open"
        )
        assert proposal["reason"]["commitment_status"] == "fulfilled"

        response = browser.post(
            f"/api/v1/findings/{card['id']}/actions",
            headers={**ORIGIN, "Idempotency-Key": "completed-close"},
            json={
                "action": "close",
                "expected_version": card["version"],
                "comment": "Результат подтверждён",
            },
        )
        assert response.status_code == 200
        closed = response.json()
        assert closed["status"] == "closed"
        assert next(
            item for item in closed["proposals"] if item["id"] == proposal["id"]
        )["status"] == "accepted"


def test_manager_decision_survives_api_restart(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser)
        with sessions.begin() as db:
            version = db.get(SettingsVersion, settings_id)
            assert version is not None
            version.created_at = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        run_pipeline(
            browser,
            sessions,
            project_id,
            key="restart-run",
            as_of="2026-09-15T10:35:00Z",
            period_start="2026-09-15T00:00:00Z",
        )
        card = next(
            browser.get(f"/api/v1/findings/{item['id']}").json()
            for item in commitment_cards(browser, project_id)
            if "смет" in item["description"]
        )
        confirmed = browser.post(
            f"/api/v1/findings/{card['id']}/actions",
            headers={**ORIGIN, "Idempotency-Key": "restart-confirm"},
            json={"action": "confirm", "expected_version": card["version"]},
        )
        assert confirmed.status_code == 200
        cookie = browser.cookies["af_session"]

    with TestClient(create_app(settings)) as browser:
        browser.cookies.set("af_session", cookie)
        restored = browser.get(f"/api/v1/findings/{card['id']}").json()
        assert restored["status"] == "confirmed"
        assert restored["actions"][0]["action"] == "confirm"
        assert restored["actions"][0]["actor"] == "manager"
        counts = browser.get(
            f"/api/v1/projects/{project_id}/findings/counts"
        ).json()
        assert counts["by_status"]["confirmed"] == 1
