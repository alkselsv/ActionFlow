import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from actionflow.application.analysis_input import TERMINAL
from actionflow.config import Settings
from actionflow.persistence.analysis import AnalysisRun, OutboxEvent
from actionflow.persistence.database import database, utc_now

MAX_ATTEMPTS = 10


async def dispatch_once(sessions, connect, queue):
    with sessions.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.status.in_(("pending", "retrying")),
                OutboxEvent.next_attempt_at <= utc_now(),
            )
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return False
        run = db.get(AnalysisRun, event.aggregate_id)
        event.attempts += 1
        execution_id = None
        try:
            client = await asyncio.wait_for(connect(), timeout=10)
            handle = await client.start_workflow(
                "AnalysisWorkflow",
                run.id,
                id=run.workflow_id,
                task_queue=queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=10),
            )
            execution_id = handle.first_execution_run_id if handle else None
        except WorkflowAlreadyStartedError as started:
            execution_id = started.run_id
        except (RPCError, OSError, TimeoutError, RuntimeError) as failure:
            event.last_error = type(failure).__name__[:64]
            if event.attempts < MAX_ATTEMPTS:
                event.status = "retrying"
                event.next_attempt_at = utc_now() + timedelta(
                    seconds=min(2 ** (event.attempts - 1), 60)
                )
                return True
            db.refresh(run, with_for_update=True)
            event.status = "failed"
            if run.status not in TERMINAL and run.status == "pending":
                run.status, run.stage = "failed", "dispatch"
                run.error_code, run.retryable = "workflow_start_failed", True
                run.finished_at = utc_now()
            return True
        db.refresh(run, with_for_update=True)
        if run.status == "pending":
            run.status, run.stage = "queued", "prepare"
        run.temporal_run_id = execution_id
        event.status, event.dispatched_at = "dispatched", utc_now()
        event.next_attempt_at = utc_now() + timedelta(seconds=10)
        event.last_error = None
        return True


async def reconcile_once(sessions, connect):
    with sessions.begin() as db:
        event = db.scalar(
            select(OutboxEvent)
            .join(AnalysisRun, AnalysisRun.id == OutboxEvent.aggregate_id)
            .where(
                OutboxEvent.status == "dispatched",
                OutboxEvent.next_attempt_at <= utc_now(),
                AnalysisRun.status.not_in(TERMINAL),
            )
            .order_by(OutboxEvent.next_attempt_at)
            .limit(1)
            .with_for_update(of=OutboxEvent, skip_locked=True)
        )
        if event is None:
            return
        event.next_attempt_at = utc_now() + timedelta(seconds=10)
        run = db.get(AnalysisRun, event.aggregate_id)
        try:
            client = await asyncio.wait_for(connect(), timeout=10)
            description = await client.get_workflow_handle(run.workflow_id).describe(
                rpc_timeout=timedelta(seconds=10)
            )
        except RPCError, OSError, TimeoutError, RuntimeError:
            return
        if description.status in {
            WorkflowExecutionStatus.FAILED,
            WorkflowExecutionStatus.TIMED_OUT,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.TERMINATED,
        }:
            db.refresh(run, with_for_update=True)
            if run.status not in TERMINAL:
                run.status, run.stage = "failed", "prepare"
                run.error_code = (
                    "analysis_input_timeout"
                    if description.status == WorkflowExecutionStatus.TIMED_OUT
                    else "analysis_input_failed"
                )
                run.retryable, run.finished_at = True, utc_now()


async def main():
    settings = Settings.from_env()
    engine, sessions = database(settings.database_url)
    client = None

    async def connect():
        nonlocal client
        if client is None:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
        return client

    try:
        while True:
            try:
                dispatched = await dispatch_once(
                    sessions, connect, settings.temporal_task_queue
                )
                await reconcile_once(sessions, connect)
            except OperationalError:
                dispatched = False
            if not dispatched:
                await asyncio.sleep(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
