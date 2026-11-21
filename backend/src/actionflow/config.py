import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    frontend_origin: str = "http://127.0.0.1:5173"
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "actionflow"

    @classmethod
    def from_env(cls):
        return cls(
            os.environ["ACTIONFLOW_DATABASE_URL"],
            temporal_address=os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "actionflow"),
        )
