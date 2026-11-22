import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


def load_local_env() -> None:
    """Load the local .env without overriding variables set by the process."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"]
    seen = set()
    for path in candidates:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _float(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _database_url() -> str:
    explicit = _optional("ACTIONFLOW_DATABASE_URL") or _optional("DATABASE_URL")
    if explicit:
        if explicit.startswith("postgres://"):
            explicit = "postgresql+psycopg://" + explicit.removeprefix("postgres://")
        elif explicit.startswith("postgresql://"):
            explicit = "postgresql+psycopg://" + explicit.removeprefix(
                "postgresql://"
            )
        return explicit
    user = quote(os.getenv("POSTGRES_USER", "postgres"), safe="")
    password = quote(os.getenv("POSTGRES_PASSWORD", "postgres"), safe="")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = quote(os.getenv("POSTGRES_DB", "actionflow"), safe="")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


@dataclass(frozen=True)
class Settings:
    database_url: str
    frontend_origin: str = "http://127.0.0.1:5173"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "actionflow"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_attempts: int = 4
    llm_budget_usd: float | None = None
    llm_input_cost_usd_per_million: float | None = None
    llm_output_cost_usd_per_million: float | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls):
        load_local_env()
        openai_key = _optional("OPENAI_API_KEY")
        openai_base_url = _optional("OPENAI_BASE_URL")
        openai_model = _optional("OPENAI_MODEL")
        return cls(
            _database_url(),
            frontend_origin=os.getenv(
                "ACTIONFLOW_FRONTEND_ORIGIN",
                os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173"),
            ),
            temporal_address=os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "actionflow"),
            llm_provider=_optional("ACTIONFLOW_LLM_PROVIDER")
            or ("openai" if openai_key else None),
            llm_model=_optional("ACTIONFLOW_LLM_MODEL") or openai_model,
            llm_api_key=_optional("ACTIONFLOW_LLM_API_KEY") or openai_key,
            llm_base_url=_optional("ACTIONFLOW_LLM_BASE_URL") or openai_base_url,
            llm_timeout_seconds=_float("ACTIONFLOW_LLM_TIMEOUT_SECONDS", 60.0),
            llm_max_attempts=_int("ACTIONFLOW_LLM_MAX_ATTEMPTS", 4),
            llm_budget_usd=_float("ACTIONFLOW_LLM_BUDGET_USD"),
            llm_input_cost_usd_per_million=_float(
                "ACTIONFLOW_LLM_INPUT_COST_USD_PER_MILLION"
            ),
            llm_output_cost_usd_per_million=_float(
                "ACTIONFLOW_LLM_OUTPUT_COST_USD_PER_MILLION"
            ),
            embedding_provider=_optional("ACTIONFLOW_EMBEDDING_PROVIDER")
            or ("openai" if openai_key and _optional("OPENAI_EMBEDDING_MODEL") else None),
            embedding_model=_optional("ACTIONFLOW_EMBEDDING_MODEL")
            or _optional("OPENAI_EMBEDDING_MODEL"),
            embedding_api_key=_optional("ACTIONFLOW_EMBEDDING_API_KEY") or openai_key,
            embedding_base_url=_optional("ACTIONFLOW_EMBEDDING_BASE_URL")
            or openai_base_url,
            embedding_timeout_seconds=_float(
                "ACTIONFLOW_EMBEDDING_TIMEOUT_SECONDS", 60.0
            ),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider and self.llm_model and self.llm_api_key)
