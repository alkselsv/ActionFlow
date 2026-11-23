"""Manage the two native Temporal schedules used by the Telegram pilot."""

import argparse
import asyncio
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from actionflow.config import Settings
from actionflow.temporal.deadlines import DEADLINE_INTERVAL

DEADLINE_ID = "actionflow-telegram-deadlines"
RECONCILIATION_ID = "actionflow-telegram-reconciliation"


def schedules(queue: str, reconciliation_minutes: int):
    checks = (
        (DEADLINE_ID, "TelegramDeadlineWindow", DEADLINE_INTERVAL, 270),
        (
            RECONCILIATION_ID,
            "TelegramReconciliation",
            timedelta(minutes=reconciliation_minutes),
            reconciliation_minutes * 60,
        ),
    )
    return {
        key: Schedule(
            action=ScheduleActionStartWorkflow(
                workflow_name,
                arg,
                id=key,
                task_queue=queue,
                execution_timeout=interval,
            ),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=interval,
                pause_on_failure=False,
            ),
        )
        for key, workflow_name, interval, arg in checks
    }


async def manage(command: str, minutes: int):
    settings = Settings.from_env()
    client = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    for identifier, schedule in schedules(
        settings.temporal_task_queue, minutes
    ).items():
        handle = client.get_schedule_handle(identifier)
        if command == "install":
            try:
                await client.create_schedule(identifier, schedule)
            except ScheduleAlreadyRunningError:
                pass
        elif command == "pause":
            await handle.pause(note="Paused by ActionFlow operator")
        elif command == "resume":
            await handle.unpause(note="Resumed by ActionFlow operator")
        description = await handle.describe()
        actual = description.schedule
        cadence = actual.spec.intervals[0].every.total_seconds() / 60
        print(
            f"{identifier}: interval={cadence:g} min, paused={actual.state.paused}, overlap=SKIP"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "pause", "resume", "status"])
    parser.add_argument("--reconciliation-minutes", type=int, default=60)
    args = parser.parse_args()
    if not 5 <= args.reconciliation_minutes <= 1440:
        parser.error("Reconciliation interval must be between 5 and 1440 minutes.")
    asyncio.run(manage(args.command, args.reconciliation_minutes))


if __name__ == "__main__":
    main()
