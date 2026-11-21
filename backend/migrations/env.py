from alembic import context

from actionflow.config import Settings
from actionflow.persistence.analysis import AnalysisRun
from actionflow.persistence.database import Base, database
from actionflow.persistence.project_settings import SettingsVersion

assert SettingsVersion.metadata is AnalysisRun.metadata is Base.metadata

settings = Settings.from_env()

if context.is_offline_mode():
    context.configure(
        url=settings.database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = database(settings.database_url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
