from network_history.config import ConfigurationError, Settings
from network_history.storage.repository import Repository


def create_repository(settings: Settings) -> Repository:
    if settings.database_backend == "sqlite":
        from network_history.storage.sqlite import SQLiteRepository

        return SQLiteRepository(settings.database_path)
    raise ConfigurationError(f"Unsupported database backend: {settings.database_backend}")
