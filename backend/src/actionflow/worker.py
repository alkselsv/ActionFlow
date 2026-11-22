import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError
from temporalio.client import Client
from temporalio.worker import Worker

from actionflow.config import Settings
from actionflow.operations import record_worker_heartbeat
from actionflow.persistence.database import database
from actionflow.temporal.activities import AnalysisActivities
from actionflow.temporal.workflow import AnalysisWorkflow

logger = logging.getLogger(__name__)


async def heartbeat_loop(sessions, queue: str) -> None:
    while True:
        try:
            await asyncio.to_thread(record_worker_heartbeat, sessions, queue)
        except SQLAlchemyError as failure:  # pragma: no cover - operational path
            logger.warning("worker_heartbeat_failed: %s", type(failure).__name__)
        await asyncio.sleep(10)


def analysis_worker(client, sessions, queue, executor, detector=None):
    activities = AnalysisActivities(sessions, detector)
    return Worker(
        client,
        task_queue=queue,
        workflows=[AnalysisWorkflow],
        activities=[
            activities.prepare,
            activities.detect,
            activities.detect_commitment,
            activities.detect_complaint,
            activities.finalize,
            activities.fail,
            activities.fail_input,
        ],
        activity_executor=executor,
        max_concurrent_activities=4,
    )


async def main():
    settings = Settings.from_env()
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    engine, sessions = database(settings.database_url)
    heartbeat = asyncio.create_task(
        heartbeat_loop(sessions, settings.temporal_task_queue)
    )
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            await analysis_worker(
                client, sessions, settings.temporal_task_queue, executor
            ).run()
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
