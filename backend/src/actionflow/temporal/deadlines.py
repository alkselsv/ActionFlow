"""Temporal timers and scheduled reconciliation for connected Telegram projects."""

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from temporalio import activity

    from actionflow.application.deadlines import advance_deadlines, reconcile

ACTIVITY_LIMIT = timedelta(seconds=20)
RETRY = RetryPolicy(maximum_attempts=3)
DEADLINE_INTERVAL = timedelta(minutes=5)
SCHEDULED_AT = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")


class DeadlineActivities:
    def __init__(self, sessions):
        self.sessions = sessions

    @activity.defn(name="advance_telegram_deadlines")
    def advance(self, at: str) -> str | None:
        return advance_deadlines(self.sessions, datetime.fromisoformat(at))

    @activity.defn(name="reconcile_telegram_projects")
    def reconcile(self, at: str, interval: int) -> int:
        timestamp = datetime.fromisoformat(at)
        return reconcile(
            self.sessions, timestamp, int(timestamp.timestamp()) // interval
        )


@workflow.defn
class TelegramDeadlineWindow:
    @workflow.run
    async def run(self, seconds: int) -> None:
        end = workflow.now() + timedelta(seconds=seconds)
        while True:
            current = workflow.now()
            upcoming = await workflow.execute_activity(
                "advance_telegram_deadlines",
                current.isoformat(),
                start_to_close_timeout=ACTIVITY_LIMIT,
                schedule_to_close_timeout=timedelta(minutes=1),
                retry_policy=RETRY,
                result_type=str | None,
            )
            if workflow.now() >= end:
                return
            ceiling = min(end, workflow.now() + timedelta(seconds=30))
            wake = (
                min(ceiling, datetime.fromisoformat(upcoming)) if upcoming else ceiling
            )
            await workflow.sleep(max(wake - workflow.now(), timedelta(milliseconds=1)))


@workflow.defn
class TelegramReconciliation:
    @workflow.run
    async def run(self, interval: int) -> int:
        info = workflow.info()
        at = info.typed_search_attributes.get(SCHEDULED_AT) or info.start_time
        return await workflow.execute_activity(
            "reconcile_telegram_projects",
            args=[at.isoformat(), interval],
            start_to_close_timeout=ACTIVITY_LIMIT,
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=RETRY,
            result_type=int,
        )
