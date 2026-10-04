from dataclasses import replace

import pytest

from network_history.storage.sqlite import SQLiteRepository


@pytest.fixture(autouse=True)
def isolated_request_log(tmp_path, monkeypatch):
    from network_history import web

    factory = web.create_request_logger

    def isolated(settings):
        if settings.request_log_path == "/data/request_log.db":
            settings = replace(settings, request_log_path=str(tmp_path / "request_log.db"))
        return factory(settings)

    monkeypatch.setattr(web, "create_request_logger", isolated)


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "history.db"))
    repo.initialize()
    return repo
