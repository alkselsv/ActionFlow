from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from actionflow.api.app import create_app
from actionflow.config import Settings
from actionflow.persistence.database import database
from actionflow.setup_manager import create_manager

ORIGIN = {"Origin": "http://127.0.0.1:5173"}


def test_manager_can_reopen_projects_after_restart(tmp_path, monkeypatch):
    settings = Settings(f"sqlite:///{tmp_path / 'actionflow.db'}")
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    migration = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    command.upgrade(migration, "head")
    command.check(migration)
    engine, sessions = database(settings.database_url)
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
        project_id = result.json()["id"]
        cookie = browser.cookies["af_session"]

    with TestClient(create_app(settings)) as browser:
        browser.cookies.set("af_session", cookie)
        projects = browser.get("/api/v1/projects").json()["items"]
        assert len(projects) == 2
        assert len({project["client"]["id"] for project in projects}) == 1
        project = browser.get(f"/api/v1/projects/{project_id}").json()
        assert project["name"] == "Second project"
        assert project["manager_name"] == "Manager"
        assert browser.post("/api/v1/auth/logout", headers=ORIGIN).status_code == 204
        browser.cookies.set("af_session", cookie)
        assert browser.get(f"/api/v1/projects/{project_id}").status_code == 401
