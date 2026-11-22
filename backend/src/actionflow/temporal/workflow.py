from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError

PREPARE_TIMEOUT = timedelta(minutes=5)
PREPARE_RETRY = RetryPolicy(maximum_attempts=3)
DETECT_TIMEOUT = timedelta(minutes=5)
DETECT_RETRY = RetryPolicy(maximum_attempts=4)
FINALIZE_TIMEOUT = timedelta(minutes=1)
FINALIZE_RETRY = RetryPolicy(maximum_attempts=3)


def failure_details(
    failure: ActivityError,
    default_code: str,
    known_retryable: tuple[str, ...] = (),
):
    cause = failure.cause
    if isinstance(cause, TimeoutError):
        return default_code, True
    if isinstance(cause, ApplicationError):
        if cause.non_retryable and cause.type:
            return cause.type, False
        if cause.type in known_retryable:
            return cause.type, True
        return default_code, True
    return default_code, True


async def record_failure(run_id: str, stage: str, code: str, retryable: bool):
    await workflow.execute_activity(
        "fail_analysis",
        args=[run_id, stage, code, retryable],
        start_to_close_timeout=timedelta(seconds=30),
        schedule_to_close_timeout=timedelta(minutes=2),
        retry_policy=RetryPolicy(maximum_attempts=3),
    )


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
            code, retryable = failure_details(
                failure,
                "analysis_input_failed",
                ("analysis_input_timeout",),
            )
            if isinstance(failure.cause, TimeoutError):
                code = "analysis_input_timeout"
            await record_failure(run_id, "prepare", code, retryable)
            raise ApplicationError(
                "Не удалось подготовить вход анализа",
                type=code,
                non_retryable=not retryable,
            ) from failure

        try:
            await workflow.execute_activity(
                "detect_unanswered",
                run_id,
                start_to_close_timeout=DETECT_TIMEOUT,
                schedule_to_close_timeout=timedelta(minutes=16),
                retry_policy=DETECT_RETRY,
            )
        except ActivityError as failure:
            code, retryable = failure_details(
                failure,
                "unanswered_detection_failed",
                ("provider_rate_limited", "provider_timeout"),
            )
            if isinstance(failure.cause, TimeoutError):
                code = "provider_timeout"
            await record_failure(run_id, "detector", code, retryable)
            raise ApplicationError(
                "Не удалось найти обращения без ответа",
                type=code,
                non_retryable=not retryable,
            ) from failure

        try:
            await workflow.execute_activity(
                "detect_commitment",
                run_id,
                start_to_close_timeout=DETECT_TIMEOUT,
                schedule_to_close_timeout=timedelta(minutes=16),
                retry_policy=DETECT_RETRY,
            )
        except ActivityError as failure:
            code, retryable = failure_details(
                failure,
                "commitment_detection_failed",
                ("provider_rate_limited", "provider_timeout"),
            )
            if isinstance(failure.cause, TimeoutError):
                code = "provider_timeout"
            await record_failure(run_id, "detector", code, retryable)
            raise ApplicationError(
                "Не удалось найти обязательства Vedita",
                type=code,
                non_retryable=not retryable,
            ) from failure

        try:
            await workflow.execute_activity(
                "finalize_analysis",
                run_id,
                start_to_close_timeout=FINALIZE_TIMEOUT,
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=FINALIZE_RETRY,
            )
        except ActivityError as failure:
            code, retryable = failure_details(failure, "analysis_finalize_failed")
            await record_failure(run_id, "finalize", code, retryable)
            raise ApplicationError(
                "Не удалось завершить анализ",
                type=code,
                non_retryable=not retryable,
            ) from failure
