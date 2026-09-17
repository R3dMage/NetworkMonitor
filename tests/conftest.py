import pytest

from network_history.storage.sqlite import SQLiteRepository


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "history.db"))
    repo.initialize()
    return repo
