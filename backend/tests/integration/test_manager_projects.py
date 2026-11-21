import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from actionflow.api.app import create_app
from actionflow.config import Settings
from actionflow.persistence.database import database
from actionflow.persistence.project_settings import Chat, Participant
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
        assert browser.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204
        browser.cookies.set("af_session", cookie)
        assert browser.get(f"/api/v1/projects/{project_id}").status_code == 401
        assert (
            browser.get(f"/api/v1/projects/{project_id}/settings/versions").status_code
            == 401
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
    assert browser.get(other_path + "/participants").json()["items"] == []
    assert (
        browser.get(other_path + "/settings/versions/" + initial["id"]).status_code
        == 404
    )
