import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

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
from actionflow.dispatcher import dispatch_once
from actionflow.persistence.database import database
from actionflow.setup_manager import create_manager
from actionflow.temporal.activities import AnalysisActivities
from actionflow.temporal.workflow import AnalysisWorkflow
from actionflow.worker import analysis_worker

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
    settings_id = settings.json()["id"]
    imported = browser.post(
        f"/api/v1/projects/{project_id}/messages/import",
        headers={**ORIGIN, "Content-Type": "application/json"},
        content=(FIXTURES / "north-conv-n-001.json").read_bytes(),
    )
    assert imported.status_code == 200
    return project_id, settings_id


def run_pipeline(
    browser, sessions, project_id, *, key, detector=None, apply_to_queue=True
):
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


def update_response_sla(browser, project_id, base_version_id, minutes):
    response = browser.post(
        f"/api/v1/projects/{project_id}/settings",
        headers=ORIGIN,
        json={
            "base_version_id": base_version_id,
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
            "first_response_minutes": minutes,
            "calendar": {"closed_dates": []},
            "participant_sides": [
                {"source_author_id": "user9001", "side": "customer"},
                {"source_author_id": "user9101", "side": "vedita"},
            ],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_saved_unanswered_card_evidence_and_idempotent_runs(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, settings_id = setup_project(browser)
        run_id = run_pipeline(browser, sessions, project_id, key="unanswered-1")

        run = browser.get(f"/api/v1/analyses/{run_id}").json()
        assert run["status"] == "succeeded"
        assert run["stage"] == "unanswered_ready"
        assert run["settings_version_id"] == settings_id
        assert run["detector"]["model_id"] == "local-baseline-unanswered-v1"
        assert run["detector"]["prompt_version"] == LocalBaselineDetector.prompt_version
        assert run["detector"]["schema_version"] == "detector_result.v1"
        assert run["cost_usd"] is None
        assert run["usage"]["cost_usd"] is None
        assert run["candidate_count"] == 1
        assert run["finding_count"] == 1
        assert run["implemented_types"] == ["unanswered", "commitment", "complaint"]

        findings = browser.get(f"/api/v1/analyses/{run_id}/findings").json()["items"]
        assert len(findings) == 1
        assert findings[0]["priority"] == "high"
        assert findings[0]["sla"]["state"] == "breached"
        assert findings[0]["limitations"]
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
        assert card["priority"] == "high"
        assert card["priority_reason"]["rule_version"] == "priority.v1"
        assert "first_response_sla_breached" in card["priority_reason"]["reasons"]
        assert card["sla"]["state"] == "breached"
        assert card["sla"]["first_response_at"].startswith("2026-09-15T13:10:00")
        assert card["sla"]["settings_version_id"] == settings_id
        assert card["sla"]["reason"]["sla_minutes"] == 60
        assert card["sla"]["due_at"].startswith("2026-09-15T10:30:00")

        queue = browser.get(
            f"/api/v1/projects/{project_id}/findings?priority=high&sla=breached"
        ).json()
        assert [item["id"] for item in queue["items"]] == [card_id]
        counts = browser.get(f"/api/v1/projects/{project_id}/findings/counts").json()
        assert counts["open"] == 1
        assert counts["sla_breaches"] == 1
        assert counts["by_sla"]["breached"] == 1

        candidates = browser.get(f"/api/v1/analyses/{run_id}/candidates").json()[
            "items"
        ]
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
        second_findings = browser.get(f"/api/v1/analyses/{second}/findings").json()[
            "items"
        ]
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


def test_changed_settings_do_not_rewrite_previous_sla(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, first_settings_id = setup_project(browser)
        first_run = run_pipeline(browser, sessions, project_id, key="sla-first")
        card_id = browser.get(f"/api/v1/analyses/{first_run}/findings").json()["items"][
            0
        ]["id"]
        first_card = browser.get(
            f"/api/v1/findings/{card_id}?run_id={first_run}"
        ).json()
        assert first_card["sla"]["state"] == "breached"
        assert first_card["sla"]["reason"]["sla_minutes"] == 60

        second_settings_id = update_response_sla(
            browser, project_id, first_settings_id, 240
        )
        assert second_settings_id != first_settings_id
        second_run = run_pipeline(browser, sessions, project_id, key="sla-second")
        second_card = browser.get(
            f"/api/v1/findings/{card_id}?run_id={second_run}"
        ).json()
        assert second_card["sla"]["state"] == "met"
        assert second_card["sla"]["reason"]["sla_minutes"] == 240
        assert second_card["priority"] == "medium"

        # The first run keeps its own calculation and settings version.
        first_card_again = browser.get(
            f"/api/v1/findings/{card_id}?run_id={first_run}"
        ).json()
        assert first_card_again["sla"]["state"] == "breached"
        assert first_card_again["sla"]["reason"]["sla_minutes"] == 60
        assert first_card_again["sla"]["settings_version_id"] == first_settings_id

        first_queue = browser.get(
            f"/api/v1/projects/{project_id}/findings?run_id={first_run}&priority=high&sla=breached"
        ).json()
        assert [item["id"] for item in first_queue["items"]] == [card_id]
        first_counts = browser.get(
            f"/api/v1/projects/{project_id}/findings/counts?run_id={first_run}"
        ).json()
        assert first_counts["sla_breaches"] == 1

        second_queue = browser.get(
            f"/api/v1/projects/{project_id}/findings?run_id={second_run}&priority=medium&sla=met"
        ).json()
        assert [item["id"] for item in second_queue["items"]] == [card_id]
        second_counts = browser.get(
            f"/api/v1/projects/{project_id}/findings/counts?run_id={second_run}"
        ).json()
        assert second_counts["sla_breaches"] == 0


def test_foreign_evidence_is_rejected_without_creating_a_card(tmp_path, monkeypatch):
    settings, sessions = setup_db(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as browser:
        project_id, _ = setup_project(browser)
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
        candidates = browser.get(f"/api/v1/analyses/{run_id}/candidates").json()[
            "items"
        ]
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
        project_id, _ = setup_project(browser)
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
        project_id, _ = setup_project(browser)
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


@pytest.mark.skipif(
    os.environ.get("ACTIONFLOW_TEST_TEMPORAL") != "1",
    reason="Set ACTIONFLOW_TEST_TEMPORAL=1 to start the Temporal test server",
)
def test_full_analysis_preserves_partial_results_and_manager_actions(
    tmp_path, monkeypatch
):
    settings, sessions = setup_db(tmp_path, monkeypatch)

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as temporal:
            with (
                TestClient(create_app(settings)) as browser,
                ThreadPoolExecutor(4) as executor,
            ):
                project_id, settings_id = setup_project(browser)
                path = f"/api/v1/projects/{project_id}/analyses"
                payload = {
                    "period_start": "2026-09-14T00:00:00Z",
                    "period_end": "2026-09-16T00:00:00Z",
                    "as_of": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                }
                queue = "full-analysis"

                async def connect():
                    return temporal.client

                async def start(key, **overrides):
                    response = browser.post(
                        path,
                        json={**payload, **overrides},
                        headers={**ORIGIN, "Idempotency-Key": key},
                    )
                    assert response.status_code == 202
                    result = response.json()
                    assert await dispatch_once(sessions, connect, queue)
                    return result["run_id"], temporal.client.get_workflow_handle(
                        result["workflow_id"]
                    )

                activities = AnalysisActivities(sessions)
                waiting = asyncio.Event()
                release = asyncio.Event()

                @activity.defn(name="detect_unanswered")
                async def acknowledge_after_retry(run_id: str):
                    await asyncio.to_thread(activities.detect, run_id)
                    if activity.info().attempt == 1:
                        raise ApplicationError("Completion was not delivered")

                @activity.defn(name="detect_commitment")
                async def interrupted(run_id: str):
                    waiting.set()
                    await release.wait()
                    raise ApplicationError("Worker restarted")

                worker = Worker(
                    temporal.client,
                    task_queue=queue,
                    workflows=[AnalysisWorkflow],
                    max_cached_workflows=0,
                    activities=[
                        activities.prepare,
                        acknowledge_after_retry,
                        interrupted,
                    ],
                    activity_executor=executor,
                )
                async with worker:
                    first, handle = await start("partial")
                    await asyncio.wait_for(waiting.wait(), 15)
                    stop = asyncio.create_task(worker.shutdown())
                    await asyncio.sleep(0)
                    release.set()
                    await stop

                @activity.defn(name="detect_commitment")
                async def provider_failure(run_id: str):
                    raise ApplicationError(
                        "Provider refused", type="provider_error", non_retryable=True
                    )

                async with Worker(
                    temporal.client,
                    task_queue=queue,
                    workflows=[AnalysisWorkflow],
                    max_cached_workflows=0,
                    activities=[provider_failure, activities.fail],
                    activity_executor=executor,
                ):
                    with pytest.raises(WorkflowFailureError):
                        await asyncio.wait_for(handle.result(), 20)
                status = browser.get(f"/api/v1/analyses/{first}").json()
                assert status["status"] == "failed"
                assert status["error_code"] == "provider_error"
                assert {
                    kind: item["status"] for kind, item in status["detectors"].items()
                } == {
                    "unanswered": "succeeded",
                    "commitment": "failed",
                    "complaint": "not_run",
                }
                [finding] = browser.get(f"/api/v1/analyses/{first}/findings").json()[
                    "items"
                ]
                card_path = f"/api/v1/findings/{finding['id']}"
                response = browser.post(
                    f"{card_path}/actions",
                    headers={**ORIGIN, "Idempotency-Key": "confirm-before-retry"},
                    json={"action": "confirm", "expected_version": finding["version"]},
                )
                assert response.status_code == 200
                new_settings = update_response_sla(
                    browser, project_id, settings_id, 240
                )
                async with analysis_worker(temporal.client, sessions, queue, executor):
                    second, handle = await start("retry")
                    await asyncio.wait_for(handle.result(), 20)
                    evaluation, handle = await start("evaluation", apply_to_queue=False)
                    await asyncio.wait_for(handle.result(), 20)
                response = browser.get(
                    f"/api/v1/analyses/{first}/comparison", params={"other": second}
                )
                assert response.status_code == 200, response.text
                comparison = response.json()
                assert comparison["before"]["settings_version_id"] == settings_id
                assert comparison["after"]["settings_version_id"] == new_settings
                assert all(
                    item["status"] == "succeeded"
                    for item in comparison["after"]["detectors"].values()
                )
                [change] = comparison["changes"]
                assert change["type"] == "unanswered"
                assert (
                    change["before"][0]["sla_due_at"]
                    != change["after"][0]["sla_due_at"]
                )
                response = browser.get(
                    f"/api/v1/analyses/{second}/comparison",
                    params={"other": evaluation},
                )
                assert response.status_code == 200
                assert response.json()["changes"] == []
                second_findings = browser.get(
                    f"/api/v1/analyses/{second}/findings"
                ).json()["items"]
                assert (
                    len(
                        [
                            item
                            for item in second_findings
                            if item["id"] == finding["id"]
                        ]
                    )
                    == 1
                )
                card = browser.get(card_path).json()
                assert card["status"] == "confirmed"
                assert len(card["actions"]) == 1

    asyncio.run(scenario())
