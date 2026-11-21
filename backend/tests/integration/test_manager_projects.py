import os
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from actionflow.api.app import create_app
from actionflow.config import Settings
from actionflow.persistence.database import database
from actionflow.persistence.project_settings import Chat, Message, Participant
from actionflow.setup_manager import create_manager

ORIGIN = {"Origin": "http://127.0.0.1:5173"}


def test_manager_can_reopen_projects_after_restart(tmp_path, monkeypatch):
    settings = Settings(
        os.environ.get("ACTIONFLOW_EMPTY_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'actionflow.db'}"
    )
    engine, sessions = database(settings.database_url)
    try:
        assert not inspect(engine).get_table_names(), (
            "Integration test requires an empty database"
        )
    finally:
        engine.dispose()
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    migration = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    command.upgrade(migration, "0001_manager_projects")
    create_manager(sessions, "manager", "synthetic-test-password")
    engine.dispose()
    with TestClient(create_app(settings)) as browser:
        assert browser.get("/api/v1/projects").status_code == 401
        assert browser.post("/api/v1/auth/logout").status_code == 401
        credentials = {"username": "manager", "password": "synthetic-test-password"}
        assert browser.post("/api/v1/auth/login", json=credentials).status_code == 400
        assert (
            browser.post(
                "/api/v1/auth/login",
                json={**credentials, "password": "incorrect"},
                headers=ORIGIN,
            ).status_code
            == 401
        )
        login = browser.post("/api/v1/auth/login", json=credentials, headers=ORIGIN)
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert browser.get("/api/v1/auth/me").json()["username"] == "manager"
        assert browser.get("/api/v1/projects").json()["items"] == []
        project_ids = []
        for name in ("First project", "Second project"):
            result = browser.post(
                "/api/v1/projects",
                json={
                    "client_name": "Test client",
                    "project_name": name,
                    "manager_name": "Manager",
                },
                headers=ORIGIN,
            )
            assert result.status_code == 201
            project_ids.append(result.json()["id"])
        other_id, project_id = project_ids
        cookie = browser.cookies["af_session"]

    command.upgrade(migration, "0003_messages")
    with sessions.begin() as db:
        chat = Chat(
            project_id=other_id,
            source="telegram",
            external_id="200006",
            name="Test",
            type="group",
        )
        db.add(chat)
        db.flush()
        author = Participant(
            chat_id=chat.id, source_author_id="user9002", display_name="Pavel"
        )
        db.add(author)
        db.flush()
        legacy = Message(
            chat_id=chat.id,
            external_id="edge-reimport-001",
            participant_id=author.id,
            text="Первый вопрос.",
            sent_at=datetime(2026, 9, 16, 13, tzinfo=UTC),
            observed_at=datetime(2026, 9, 17, tzinfo=UTC),
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
    command.upgrade(migration, "head")
    command.check(migration)
    with TestClient(create_app(settings)) as browser:
        browser.cookies.set("af_session", cookie)
        projects = browser.get("/api/v1/projects").json()["items"]
        assert len(projects) == 2
        assert len({project["client"]["id"] for project in projects}) == 1
        project = browser.get(f"/api/v1/projects/{project_id}").json()
        assert project["name"] == "Second project"
        assert project["manager_name"] == "Manager"
        check_settings(browser, sessions, project_id, other_id)
        check_repeat_import(browser, other_id, legacy_id)
        assert browser.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204
        browser.cookies.set("af_session", cookie)
        assert browser.get(f"/api/v1/projects/{project_id}").status_code == 401
        assert (
            browser.get(f"/api/v1/projects/{project_id}/settings/versions").status_code
            == 401
        )


def check_repeat_import(browser, project_id, legacy_id):
    fixtures = Path(__file__).resolve().parents[3] / "data/fixtures/telegram"
    path = f"/api/v1/projects/{project_id}/messages"
    first = (fixtures / "edge-reimport-s-006-first.json").read_bytes()
    second = (fixtures / "edge-reimport-s-006-second.json").read_bytes()
    headers = {**ORIGIN, "Content-Type": "application/json"}
    migrated = browser.get(path).json()["items"][0]
    assert migrated["id"] == legacy_id
    assert migrated["revision_no"] == 1
    assert migrated["source_observed_at"] is None
    created = browser.post(path + "/import", headers=headers, content=first)
    assert created.status_code == 200
    initial_items = browser.get(path).json()["items"]
    original = initial_items[-1]
    assert len(initial_items) == 3
    assert initial_items[0]["revision_id"] == migrated["revision_id"]
    assert initial_items[0]["observed_at"] == migrated["observed_at"]
    assert initial_items[0]["source_observed_at"] is not None
    assert (
        browser.post(path + "/import", headers=headers, content=first).json()[
            "imported"
        ]
        == 0
    )
    retry_headers = {**headers, "Idempotency-Key": "re-import-test"}
    assert (
        browser.post(path + "/import", headers=retry_headers, content=b"{").status_code
        == 400
    )
    changed = browser.post(path + "/import", headers=retry_headers, content=second)
    assert changed.json()["revisions_created"] == 1
    assert changed.json()["imported"] == 1
    replay = browser.post(path + "/import", headers=retry_headers, content=second)
    assert replay.json() == changed.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert (
        browser.post(path + "/import", headers=retry_headers, content=first).status_code
        == 409
    )
    partial = browser.post(path + "/import", headers=headers, content=first)
    assert partial.json()["missing_from_export"] == 1
    current = browser.get(path).json()["items"]
    assert len(current) == 4
    assert current[2]["id"] == original["id"]
    assert current[2]["text"] != original["text"]
    assert current[3]["reply_to_external_id"] == original["external_id"]
    assert current[2]["edited_at"] is not None
    assert current[2]["source_observed_at"] is not None
    assert (
        browser.get(f"{path}/{original['id']}/revisions").json()["items"][0]["text"]
        == original["text"]
    )
    for fixture in ("edge-media-s-003", "edge-service-s-004"):
        project = browser.post(
            "/api/v1/projects",
            headers=ORIGIN,
            json={
                "client_name": "Test client",
                "project_name": fixture,
                "manager_name": "Manager",
            },
        )
        assert project.status_code == 201
        edge_path = f"/api/v1/projects/{project.json()['id']}/messages"
        imported = browser.post(
            edge_path + "/import",
            headers=headers,
            content=(fixtures / f"{fixture}.json").read_bytes(),
        )
        assert imported.status_code == 200
        messages = browser.get(edge_path).json()["items"]
        if fixture.startswith("edge-media"):
            assert any(
                item["attachments"].get("voice_message") and item["text"] == ""
                for item in messages
            )
            assert imported.json()["attachments_present"] > 0
        else:
            assert imported.json()["system_events"] == 4
            assert len(messages) == 5
            assert all(
                item["source_type"] == "service" and item["author"]["side"] is None
                for item in messages
                if item["external_id"] != "edge-service-003"
            )
            assert (
                next(
                    item
                    for item in messages
                    if item["external_id"] == "edge-service-003"
                )["source_type"]
                == "message"
            )


def check_settings(browser, sessions, project_id, other_id):
    project_path = f"/api/v1/projects/{project_id}"
    path = project_path + "/settings"
    before_import = {
        "context_text": "Synthetic context",
        "agreements": "Confirm requests during working hours",
        "manager_name": "New manager",
        "client_type": "north",
        "work_hours": {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"},
        "timezone": "Europe/Moscow",
        "first_response_minutes": 60,
        "calendar": {"closed_dates": ["2026-11-23"]},
        "participant_sides": [{"source_author_id": "user9001", "side": "vedita"}],
    }
    assert browser.get(path).status_code == 404
    for field, invalid in (
        ("timezone", "Invalid/Zone"),
        ("calendar", {"closed_dates": ["2026-02-30"]}),
        ("calendar", {"closed_dates": [0]}),
        ("work_hours", {"weekdays": [1, 1], "start": "18:00", "end": "09:00"}),
    ):
        response = browser.post(
            path, headers=ORIGIN, json={**before_import, field: invalid}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"
    assert browser.get(path + "/versions").json()["items"] == []

    first = browser.post(path, json=before_import, headers=ORIGIN)
    assert first.status_code == 201
    initial = first.json()
    assert initial["version_no"] == 1
    assert browser.get(project_path).json()["manager_name"] == "New manager"
    assert browser.get(project_path + "/participants").json()["items"] == [
        {
            "source_author_id": "user9001",
            "display_name": None,
            "imported": False,
            "side": "vedita",
        }
    ]
    # Simulate the persisted output of the future importer with matching display names.
    with sessions.begin() as db:
        chat = Chat(
            project_id=project_id,
            source="telegram",
            external_id="test-chat",
            name="Test",
            type="group",
        )
        db.add(chat)
        db.flush()
        db.add_all(
            [
                Participant(
                    chat_id=chat.id,
                    source_author_id=author_id,
                    display_name="Same name",
                )
                for author_id in ("user9001", "user9002")
            ]
        )
    participants = browser.get(project_path + "/participants").json()["items"]
    assert [(row["source_author_id"], row["side"]) for row in participants] == [
        ("user9001", "vedita"),
        ("user9002", "unknown"),
    ]
    after_import = {
        **before_import,
        "base_version_id": initial["id"],
        "participant_sides": [
            {"source_author_id": "user9001", "side": "vedita"},
            {"source_author_id": "user9002", "side": "customer"},
        ],
        "context_text": "Updated context",
    }
    second = browser.post(path, json=after_import, headers=ORIGIN)
    assert second.status_code == 201
    assert browser.get(path).json() == second.json()
    assert second.json()["version_no"] == 2
    assert browser.post(path, json=after_import, headers=ORIGIN).status_code == 409
    assert browser.get(path + "/versions/" + initial["id"]).json() == initial
    assert browser.get(path + "/versions?limit=1&cursor=2").json()["items"] == [initial]
    other_path = f"/api/v1/projects/{other_id}"
    assert browser.get(other_path + "/participants").json()["items"] == [
        {
            "source_author_id": "user9002",
            "display_name": "Pavel",
            "imported": True,
            "side": "unknown",
        }
    ]
    assert (
        browser.get(other_path + "/settings/versions/" + initial["id"]).status_code
        == 404
    )
