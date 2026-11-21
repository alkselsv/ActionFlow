from temporalio import activity
from temporalio.exceptions import ApplicationError

from actionflow.application.analysis_input import InputTooLarge, fail_run, prepare_input
from actionflow.temporal.workflow import PREPARE_TIMEOUT


class AnalysisActivities:
    def __init__(self, sessions):
        self.sessions = sessions

    @activity.defn(name="prepare_analysis_input")
    def prepare(self, run_id: str) -> None:
        try:
            prepare_input(
                self.sessions, run_id, activity.info().started_time + PREPARE_TIMEOUT
            )
        except TimeoutError:
            raise ApplicationError(
                "Истекло время подготовки", type="analysis_input_timeout"
            ) from None
        except InputTooLarge:
            raise ApplicationError(
                "В срезе больше 50 000 сообщений",
                type="analysis_input_too_large",
                non_retryable=True,
            ) from None
        except ValueError:
            raise ApplicationError(
                "Запуск анализа не найден",
                type="analysis_run_not_found",
                non_retryable=True,
            ) from None

    @activity.defn(name="fail_analysis_input")
    def fail(self, run_id: str, code: str, retryable: bool) -> None:
        fail_run(self.sessions, run_id, "prepare", code, retryable)
