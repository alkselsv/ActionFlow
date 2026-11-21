from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError

PREPARE_TIMEOUT = timedelta(minutes=5)
PREPARE_RETRY = RetryPolicy(maximum_attempts=3)


@workflow.defn(name="AnalysisWorkflow")
class AnalysisWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        try:
            await workflow.execute_activity(
                "prepare_analysis_input",
                run_id,
                start_to_close_timeout=PREPARE_TIMEOUT,
                schedule_to_close_timeout=timedelta(minutes=16),
                retry_policy=PREPARE_RETRY,
            )
        except ActivityError as failure:
            cause = failure.cause
            if isinstance(cause, TimeoutError) or (
                isinstance(cause, ApplicationError)
                and cause.type == "analysis_input_timeout"
            ):
                code, retryable = "analysis_input_timeout", True
            elif isinstance(cause, ApplicationError) and cause.non_retryable:
                code, retryable = cause.type or "analysis_input_failed", False
            else:
                code, retryable = "analysis_input_failed", True
            await workflow.execute_activity(
                "fail_analysis_input",
                args=[run_id, code, retryable],
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise ApplicationError(
                "Не удалось подготовить вход анализа",
                type=code,
                non_retryable=not retryable,
            ) from failure
