from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from actionflow.api.app import create_app
from actionflow.config import Settings
from actionflow.operations import provider_status, record_worker_heartbeat
from actionflow.ops import main as ops_main
from actionflow.persistence.analysis import AnalysisRun, OutboxEvent
from actionflow.persistence.database import WorkerHeartbeat, database, new_id
from actionflow.setup_manager import create_manager

ORIGIN = {"Origin": "http://127.0.0.1:5173"}
PASSWORD = "synthetic-operations-password"


def test_diagnostics_report_run_worker_and_provider_without_content(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'operations.db'}")
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    migration = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    command.upgrade(migration, "head")
    _, sessions = database(settings.database_url)
    create_manager(sessions, "manager", PASSWORD)

    with TestClient(create_app(settings)) as browser:
        assert browser.get("/api/v1/diagnostics").status_code == 401
        login = browser.post(
            "/api/v1/auth/login",
            json={"username": "manager", "password": PASSWORD},
            headers=ORIGIN,
        )
        assert login.status_code == 200
        project = browser.post(
            "/api/v1/projects",
            json={
                "client_name": "Synthetic operations client",
                "project_name": "Synthetic operations project",
                "manager_name": "Manager",
            },
            headers=ORIGIN,
        )
        assert project.status_code == 201
        project_id = project.json()["id"]

        run_id = new_id()
        with sessions.begin() as db:
            db.add(
                AnalysisRun(
                    id=run_id,
                    project_id=project_id,
                    period_start=datetime(2026, 11, 20, 9, tzinfo=UTC),
                    period_end=datetime(2026, 11, 20, 10, tzinfo=UTC),
                    as_of=datetime(2026, 11, 20, 10, tzinfo=UTC),
                    apply_to_queue=True,
                    status="failed",
                    stage="detector",
                    request_hash="a" * 64,
                    workflow_id=f"analysis:{run_id}",
                    error_code="provider_timeout",
                    retryable=True,
                )
            )
            db.add(
                OutboxEvent(
                    aggregate_id=run_id,
                    event_key=run_id,
                    status="retrying",
                    attempts=3,
                    last_error="RPCError",
                )
            )

        service = browser.get("/api/v1/diagnostics")
        assert service.status_code == 200
        service_body = service.json()
        assert service_body["database"] == {"available": True}
        assert service_body["worker"]["available"] is False
        assert service_body["provider"]["mode"] == "local_baseline"
        assert service_body["provider"]["available"] is True
        assert "content_policy" in service_body["logging"]

        assert browser.get(f"/api/v1/analyses/{new_id()}/diagnostics").status_code == 404
        diagnostics = browser.get(f"/api/v1/analyses/{run_id}/diagnostics")
        assert diagnostics.status_code == 200
        body = diagnostics.json()
        assert body["run"]["status"] == "failed"
        assert body["run"]["error_code"] == "provider_timeout"
        assert body["run"]["retryable"] is True
        assert body["outbox"][0]["attempts"] == 3
        assert body["outbox"][0]["last_error"] == "RPCError"
        assert body["worker"]["available"] is False

        record_worker_heartbeat(sessions, "actionflow", name="worker")
        live = browser.get("/api/v1/diagnostics").json()
        assert live["worker"]["available"] is True
        assert live["worker"]["task_queue"] == "actionflow"
        assert "last_seen_at" in live["worker"]

        with sessions.begin() as db:
            heartbeat = db.get(WorkerHeartbeat, "worker")
            heartbeat.last_seen_at = datetime.now(UTC) - timedelta(minutes=2)
        stale = browser.get("/api/v1/diagnostics").json()
        assert stale["worker"]["available"] is False
        assert stale["worker"]["reason"] == "stale"

    assert ops_main(["run", run_id]) == 0
    output = capsys.readouterr().out
    assert run_id in output
    assert "provider_timeout" in output
    assert "Synthetic operations" not in output


def test_external_provider_probe_sends_no_conversation(monkeypatch):
    settings = Settings(
        database_url="sqlite://",
        llm_provider="openai-compatible",
        llm_model="synthetic-model",
        llm_api_key="synthetic-key",
        llm_base_url="http://provider.invalid/v1",
    )
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("actionflow.operations.urlopen", fake_urlopen)
    result = provider_status(settings, probe=True)
    assert result["available"] is True
    assert result["checked"] is True
    assert captured["url"] == "http://provider.invalid/v1/models"
    assert captured["body"] is None
    assert captured["timeout"] == 10
