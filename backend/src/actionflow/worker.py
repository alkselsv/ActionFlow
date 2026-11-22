import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from actionflow.config import Settings
from actionflow.persistence.database import database
from actionflow.temporal.activities import AnalysisActivities
from actionflow.temporal.workflow import AnalysisWorkflow


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
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            await analysis_worker(
                client, sessions, settings.temporal_task_queue, executor
            ).run()
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
