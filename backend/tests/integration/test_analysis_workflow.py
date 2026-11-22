import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from actionflow.temporal import workflow


@pytest.mark.skipif(
    os.environ.get("ACTIONFLOW_TEST_TEMPORAL") != "1",
    reason="Set ACTIONFLOW_TEST_TEMPORAL=1 to start the Temporal test server",
)
def test_analysis_workflow_bounds_retries_and_reports_timeout(monkeypatch):
    monkeypatch.setattr(workflow, "PREPARE_TIMEOUT", timedelta(milliseconds=100))
    attempts = {}
    errors = {}

    @activity.defn(name="prepare_analysis_input")
    async def prepare(run_id: str):
        attempts[run_id] = attempts.get(run_id, 0) + 1
        if run_id == "permanent":
            raise ApplicationError(
                "Invalid input", type="analysis_input_too_large", non_retryable=True
            )
        if run_id == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("Unavailable")

    @activity.defn(name="fail_analysis")
    async def fail(run_id: str, stage: str, code: str, retryable: bool):
        errors[run_id] = (stage, code, retryable)

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as temporal:
            queue = f"analysis-test-{uuid4()}"
            async with Worker(
                temporal.client,
                task_queue=queue,
                workflows=[workflow.AnalysisWorkflow],
                activities=[prepare, fail],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                for case in ("transient", "permanent", "timeout"):
                    with pytest.raises(WorkflowFailureError):
                        await asyncio.wait_for(
                            temporal.client.execute_workflow(
                                workflow.AnalysisWorkflow.run,
                                case,
                                id=f"{queue}-{case}",
                                task_queue=queue,
                            ),
                            20,
                        )

    asyncio.run(scenario())
    assert attempts == {"transient": 3, "permanent": 1, "timeout": 3}
    assert errors == {
        "transient": ("prepare", "analysis_input_failed", True),
        "permanent": ("prepare", "analysis_input_too_large", False),
        "timeout": ("prepare", "analysis_input_timeout", True),
    }


@pytest.mark.skipif(
    os.environ.get("ACTIONFLOW_TEST_TEMPORAL") != "1",
    reason="Set ACTIONFLOW_TEST_TEMPORAL=1 to start the Temporal test server",
)
def test_workflow_records_detector_failure_and_skips_finalize():
    calls = []
    failures = []

    @activity.defn(name="prepare_analysis_input")
    async def prepare(run_id: str):
        calls.append("prepare")

    @activity.defn(name="detect_unanswered")
    async def detect(run_id: str):
        calls.append("detect")
        raise ApplicationError(
            "Provider rejected the model", type="provider_error", non_retryable=True
        )

    @activity.defn(name="finalize_analysis")
    async def finalize(run_id: str):
        calls.append("finalize")

    @activity.defn(name="fail_analysis")
    async def fail(run_id: str, stage: str, code: str, retryable: bool):
        failures.append((stage, code, retryable))

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as temporal:
            queue = f"analysis-test-{uuid4()}"
            async with Worker(
                temporal.client,
                task_queue=queue,
                workflows=[workflow.AnalysisWorkflow],
                activities=[prepare, detect, finalize, fail],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(
                        temporal.client.execute_workflow(
                            workflow.AnalysisWorkflow.run,
                            "detector-failure",
                            id=f"{queue}-detector-failure",
                            task_queue=queue,
                        ),
                        20,
                    )

    asyncio.run(scenario())
    assert calls == ["prepare", "detect"]
    assert failures == [("detector", "provider_error", False)]
