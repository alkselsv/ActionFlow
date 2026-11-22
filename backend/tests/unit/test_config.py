from actionflow.config import Settings


def test_settings_builds_database_url_from_standard_postgres_vars(monkeypatch):
    for name in (
        "ACTIONFLOW_DATABASE_URL",
        "DATABASE_URL",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")
    monkeypatch.setenv("POSTGRES_DB", "actionflow")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    settings = Settings.from_env()

    assert (
        settings.database_url
        == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/actionflow"
    )
    assert settings.llm_provider == "openai"
    assert settings.llm_model == "test-model"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_base_url == "https://example.test/v1"
    assert settings.llm_configured is True


def test_explicit_database_url_wins_and_openai_defaults_are_empty(monkeypatch):
    monkeypatch.setenv(
        "ACTIONFLOW_DATABASE_URL",
        "postgresql+psycopg://custom:secret@db.example:6543/custom",
    )
    monkeypatch.setenv("POSTGRES_USER", "ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_MODEL", "")

    settings = Settings.from_env()

    assert settings.database_url.endswith("@db.example:6543/custom")
    assert settings.llm_configured is False
