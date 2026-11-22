import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from temporalio.exceptions import ApplicationError

from actionflow.api.app import create_app
from actionflow.application import analysis_input
from actionflow.config import Settings
from actionflow.detectors.contracts import (
    DetectorPermanent,
    DetectorRateLimited,
    DetectorTimeout,
    Evidence,
)
from actionflow.detectors.local_baseline import LocalBaselineDetector
from actionflow.persistence.database import database
from actionflow.setup_manager import create_manager
from actionflow.temporal.activities import AnalysisActivities

ORIGIN = {"Origin": "http://127.0.0.1:5173"}
FIXTURES = Path(__file__).resolve().parents[3] / "data/fixtures/telegram"
PASSWORD = "synthetic-test-password"


class RaisingDetector(LocalBaselineDetector):
    error = None

    def detect(self, request):
        raise self.error


class ForeignEvidenceDetector(LocalBaselineDetector):
    def detect(self, request):
        result = super().detect(request)
        result.candidates[0].evidence.append(
            Evidence(
                role="supporting",
                message_id="foreign-message",
                revision_id="foreign-revision",
                quote="чужое основание",
            )
        )
        return result


class InvalidOutputDetector(LocalBaselineDetector):
    def detect(self, request):
        return {"unexpected": True}


def setup_db(tmp_path, monkeypatch):
    settings = Settings(
        os.environ.get("ACTIONFLOW_CARD_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path / 'unanswered.db'}"
    )
    monkeypatch.setenv("ACTIONFLOW_DATABASE_URL", settings.database_url)
    config = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    command.upgrade(config, "head")
    _, sessions = database(settings.database_url)
    create_manager(sessions, "manager", PASSWORD)
    return settings, sessions


def setup_project(browser):
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
            "project_name": "Unanswered project",
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
            ],
        },
    )
    assert settings.status_code == 201
    imported = browser.post(
        f"/api/v1/projects/{project_id}/messages/import",
        headers={**ORIGIN, "Content-Type": "application/json"},
        content=(FIXTURES / "north-conv-n-001.json").read_bytes(),
    )
    assert imported.status_code == 200
    return project_id


def run_pipeline(browser, sessions, project_id, *, key, detector=None, apply_to_queue=True):
    response = browser.post(
        f"/api/v1/projects/{project_id}/analyses",
        headers={**ORIGIN, "Idempotency-Key": key},
        json={
            "period_start": "2026-09-14T00:00:00Z",
            "period_end": "2026-09-16T00:00:00Z",
            "as_of": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "apply_to_queue": apply_to_queue,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    analysis_input.prepare_input(sessions, run_id, finalize=False)
    activities = AnalysisActivities(sessions, detector or LocalBaselineDetector())
    activities.detect(run_id)
    activities.detect(run_id)
    activities.finalize(run_id)
    return run_id


def test_saved_unanswered_card_evidence_and_idempotent_runs(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id = setup_project(browser)
        run_id = run_pipeline(browser, sessions, project_id, key="unanswered-1")

        run = browser.get(f"/api/v1/analyses/{run_id}").json()
        assert run["status"] == "succeeded"
        assert run["stage"] == "unanswered_ready"
        assert run["detector"]["model_id"] == "local-baseline-unanswered-v1"
        assert run["detector"]["prompt_version"] == "unanswered-rules-v1"
        assert run["detector"]["schema_version"] == "detector_result.v1"
        assert run["cost_usd"] is None
        assert run["usage"]["cost_usd"] is None
        assert run["candidate_count"] == 1
        assert run["finding_count"] == 1
        assert run["implemented_types"] == ["unanswered"]

        findings = browser.get(f"/api/v1/analyses/{run_id}/findings").json()["items"]
        assert len(findings) == 1
        card_id = findings[0]["id"]
        card = browser.get(f"/api/v1/findings/{card_id}?run_id={run_id}").json()
        assert card["type"] == "unanswered"
        assert "Клиент ждёт решения" in card["description"]
        assert card["recommendation"] == "reply"
        assert card["evidence"][0]["role"] == "anchor"
        assert (
            card["evidence"][0]["text"]
            == "Когда сможете прислать исправленный файл? Ждём ответ."
        )
        assert any(
            item["external_id"] == "n-001-013"
            for item in card["surrounding"]
            if item["is_anchor"] is False
        )
        assert card["history"][0]["run_id"] == run_id
        assert card["detector"]["cost_usd"] is None

        candidates = browser.get(f"/api/v1/analyses/{run_id}/candidates").json()["items"]
        assert len(candidates) == 1
        assert candidates[0]["status"] == "valid"
        assert candidates[0]["evidence"][0]["role"] == "anchor"

        # Repeating the activity did not add a second candidate or observation.
        activities = AnalysisActivities(sessions)
        activities.detect(run_id)
        assert (
            len(browser.get(f"/api/v1/analyses/{run_id}/candidates").json()["items"])
            == 1
        )
        assert len(browser.get(f"/api/v1/findings/{card_id}").json()["history"]) == 1

        # A deliberate new run keeps the card identity and stores a new observation.
        second = run_pipeline(browser, sessions, project_id, key="unanswered-2")
        second_findings = browser.get(
            f"/api/v1/analyses/{second}/findings"
        ).json()["items"]
        assert second_findings[0]["id"] == card_id
        card2 = browser.get(f"/api/v1/findings/{card_id}?run_id={second}").json()
        assert [item["run_id"] for item in card2["history"]] == [run_id, second]
        assert (
            len(browser.get(f"/api/v1/projects/{project_id}/findings").json()["items"])
            == 1
        )

        # A comparison run keeps candidates but does not change the queue.
        compare = run_pipeline(
            browser,
            sessions,
            project_id,
            key="unanswered-compare",
            apply_to_queue=False,
        )
        assert browser.get(f"/api/v1/analyses/{compare}/findings").json()["items"] == []
        assert (
            len(browser.get(f"/api/v1/analyses/{compare}/candidates").json()["items"])
            == 1
        )
        assert (
            len(browser.get(f"/api/v1/projects/{project_id}/findings").json()["items"])
            == 1
        )


def test_foreign_evidence_is_rejected_without_creating_a_card(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id = setup_project(browser)
        run_id = run_pipeline(
            browser,
            sessions,
            project_id,
            key="foreign-evidence",
            detector=ForeignEvidenceDetector(),
        )
        run = browser.get(f"/api/v1/analyses/{run_id}").json()
        assert run["status"] == "succeeded"
        assert run["candidate_count"] == 1
        assert run["invalid_candidate_count"] == 1
        assert run["finding_count"] == 0
        candidates = browser.get(f"/api/v1/analyses/{run_id}/candidates").json()["items"]
        assert candidates[0]["status"] == "invalid"
        assert any(
            "evidence_not_in_input" in item
            for item in candidates[0]["validation_errors"]
        )
        assert browser.get(f"/api/v1/analyses/{run_id}/findings").json()["items"] == []


@pytest.mark.parametrize(
    "error, code, retryable",
    [
        (DetectorRateLimited(), "provider_rate_limited", True),
        (DetectorTimeout(), "provider_timeout", True),
        (DetectorPermanent(), "provider_error", False),
    ],
)
def test_detector_errors_are_classified_for_temporal(
    tmp_path, monkeypatch, error, code, retryable
):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id = setup_project(browser)
        response = browser.post(
            f"/api/v1/projects/{project_id}/analyses",
            headers={**ORIGIN, "Idempotency-Key": "provider-" + code},
            json={
                "period_start": "2026-09-14T00:00:00Z",
                "period_end": "2026-09-16T00:00:00Z",
                "as_of": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )
        run_id = response.json()["run_id"]
        analysis_input.prepare_input(sessions, run_id, finalize=False)
        detector = RaisingDetector()
        detector.error = error
        activities = AnalysisActivities(sessions, detector)
        with pytest.raises(ApplicationError) as raised:
            activities.detect(run_id)
        assert raised.value.type == code
        assert raised.value.non_retryable is not retryable


def test_invalid_model_output_is_non_retryable(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id = setup_project(browser)
        response = browser.post(
            f"/api/v1/projects/{project_id}/analyses",
            headers={**ORIGIN, "Idempotency-Key": "invalid-output"},
            json={
                "period_start": "2026-09-14T00:00:00Z",
                "period_end": "2026-09-16T00:00:00Z",
                "as_of": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            },
        )
        run_id = response.json()["run_id"]
        analysis_input.prepare_input(sessions, run_id, finalize=False)
        with pytest.raises(ApplicationError) as raised:
            AnalysisActivities(sessions, InvalidOutputDetector()).detect(run_id)
        assert raised.value.type == "detector_invalid_output"
        assert raised.value.non_retryable is True
