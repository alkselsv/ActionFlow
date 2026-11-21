import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    frontend_origin: str = "http://127.0.0.1:5173"

    @classmethod
    def from_env(cls):
        return cls(os.environ["ACTIONFLOW_DATABASE_URL"])
