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

    @activity.defn(name="fail_analysis_input")
    async def fail(run_id: str, code: str, retryable: bool):
        errors[run_id] = code, retryable

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
        "transient": ("analysis_input_failed", True),
        "permanent": ("analysis_input_too_large", False),
        "timeout": ("analysis_input_timeout", True),
    }
