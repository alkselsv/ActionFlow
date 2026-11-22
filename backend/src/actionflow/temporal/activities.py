from temporalio import activity
from temporalio.exceptions import ApplicationError

from actionflow.application.analysis_input import InputTooLarge, fail_run, prepare_input
from actionflow.application.commitments import analyze_commitments
from actionflow.application.unanswered import analyze_unanswered, finalize_run
from actionflow.detectors.contracts import (
    Detector,
    DetectorPermanent,
    DetectorRateLimited,
    DetectorTimeout,
    InvalidDetectorOutput,
)
from actionflow.detectors.local_baseline import LocalBaselineDetector
from actionflow.detectors.local_commitments import LocalCommitmentDetector
from actionflow.temporal.workflow import DETECT_TIMEOUT, PREPARE_TIMEOUT


class AnalysisActivities:
    def __init__(
        self,
        sessions,
        detector: Detector | None = None,
        commitment_detector: Detector | None = None,
    ):
        self.sessions = sessions
        self.detector = detector or LocalBaselineDetector()
        self.commitment_detector = (
            commitment_detector or LocalCommitmentDetector()
        )

    @activity.defn(name="prepare_analysis_input")
    def prepare(self, run_id: str) -> None:
        try:
            prepare_input(
                self.sessions,
                run_id,
                activity.info().started_time + PREPARE_TIMEOUT
                if activity.in_activity()
                else None,
                finalize=False,
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

    @activity.defn(name="detect_unanswered")
    def detect(self, run_id: str) -> None:
        try:
            analyze_unanswered(
                self.sessions,
                self.detector,
                run_id,
                deadline=activity.info().started_time + DETECT_TIMEOUT
                if activity.in_activity()
                else None,
            )
        except DetectorRateLimited:
            raise ApplicationError(
                "Провайдер ограничил частоту запросов",
                type="provider_rate_limited",
            ) from None
        except DetectorTimeout:
            raise ApplicationError(
                "Провайдер не ответил вовремя", type="provider_timeout"
            ) from None
        except InvalidDetectorOutput:
            raise ApplicationError(
                "Модель вернула невалидный структурированный ответ",
                type="detector_invalid_output",
                non_retryable=True,
            ) from None
        except DetectorPermanent:
            raise ApplicationError(
                "Провайдер вернул постоянную ошибку",
                type="provider_error",
                non_retryable=True,
            ) from None
        except ValueError:
            raise ApplicationError(
                "Запуск анализа не найден",
                type="analysis_run_not_found",
                non_retryable=True,
            ) from None

    @activity.defn(name="detect_commitment")
    def detect_commitment(self, run_id: str) -> None:
        try:
            analyze_commitments(
                self.sessions,
                self.commitment_detector,
                run_id,
                deadline=activity.info().started_time + DETECT_TIMEOUT
                if activity.in_activity()
                else None,
            )
        except DetectorRateLimited:
            raise ApplicationError(
                "Провайдер ограничил частоту запросов",
                type="provider_rate_limited",
            ) from None
        except DetectorTimeout:
            raise ApplicationError(
                "Провайдер не ответил вовремя", type="provider_timeout"
            ) from None
        except InvalidDetectorOutput:
            raise ApplicationError(
                "Модель вернула невалидный структурированный ответ",
                type="detector_invalid_output",
                non_retryable=True,
            ) from None
        except DetectorPermanent:
            raise ApplicationError(
                "Провайдер вернул постоянную ошибку",
                type="provider_error",
                non_retryable=True,
            ) from None
        except ValueError:
            raise ApplicationError(
                "Запуск анализа не найден",
                type="analysis_run_not_found",
                non_retryable=True,
            ) from None

    @activity.defn(name="finalize_analysis")
    def finalize(self, run_id: str) -> None:
        finalize_run(self.sessions, run_id)
    @activity.defn(name="fail_analysis")
    def fail(self, run_id: str, stage: str, code: str, retryable: bool) -> None:
        fail_run(self.sessions, run_id, stage, code, retryable)

    @activity.defn(name="fail_analysis_input")
    def fail_input(self, run_id: str, code: str, retryable: bool) -> None:
        """Compatibility alias used by the input-only workflow tests."""
        fail_run(self.sessions, run_id, "prepare", code, retryable)
