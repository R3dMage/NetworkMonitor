from unittest.mock import Mock

from network_history.collector import collect_once
from network_history.models import HistoryRow


def test_boundaries_duplicates_and_resume(repository):
    router_rows = [
        HistoryRow("aa", 100, "checkpoint.example"),
        HistoryRow("aa", 101, "same.example"),
        HistoryRow("aa", 101, "same.example"),
        HistoryRow("bb", 200, "cutoff.example"),
        HistoryRow("bb", 201, "unsettled.example"),
    ]
    repository.commit_import(router_rows[:1], 0, 100)

    class Source:
        def fetch(self, checkpoint, cutoff):
            assert (checkpoint, cutoff) == (100, 200)
            return [row for row in router_rows if checkpoint < row.timestamp <= cutoff]

    assert collect_once(repository, Source(), 300, clock=lambda: 500) == "success"
    state = repository.get_state()
    assert state.last_imported_timestamp == 200
    assert state.last_scrape_row_count == 3
    activity = repository.recent_activity()
    assert len(activity) == 4
    assert [row.url for row in activity].count("same.example") == 2
    assert len({row.id for row in activity}) == 4

    source = Mock()
    source.fetch.return_value = []
    assert collect_once(repository, source, 300, clock=lambda: 500) == "success"
    source.fetch.assert_called_once_with(200, 200)
    assert len(repository.recent_activity()) == 4


def test_empty_success_does_not_advance_checkpoint_and_clears_error(repository):
    repository.commit_import([HistoryRow("aa", 12, "x")], 0, 50)
    repository.record_error("old failure")
    source = Mock()
    source.fetch.return_value = []
    assert collect_once(repository, source, 10, clock=lambda: 100) == "success"
    state = repository.get_state()
    assert state.last_imported_timestamp == 12
    assert state.last_successful_scrape_at == 100
    assert state.last_scrape_row_count == 0
    assert state.last_error is None


def test_failed_scrape_preserves_previous_success_and_retries_same_window(repository):
    repository.commit_import([HistoryRow("aa", 12, "x")], 0, 50)
    source = Mock()
    source.fetch.side_effect = RuntimeError("router unavailable")
    assert collect_once(repository, source, 10, clock=lambda: 100) == "failed"
    state = repository.get_state()
    assert state.last_imported_timestamp == 12
    assert state.last_successful_scrape_at == 50
    assert state.last_scrape_row_count == 1
    assert "router unavailable" in state.last_error
    source.fetch.side_effect = None
    source.fetch.return_value = [HistoryRow("aa", 90, "recovered")]
    assert collect_once(repository, source, 10, clock=lambda: 100) == "success"
    assert source.fetch.call_args.args == (12, 90)
    assert repository.get_state().last_error is None


def test_concurrent_invocation_skips_before_fetch(repository):
    source = Mock()
    with repository.collection_lock() as acquired:
        assert acquired
        assert collect_once(repository, source, 0) == "skipped"
        assert repository.collection_running()
    source.fetch.assert_not_called()
    assert not repository.collection_running()


def test_out_of_window_result_cannot_advance_checkpoint(repository):
    source = Mock()
    source.fetch.return_value = [HistoryRow("aa", 100, "too-new")]
    assert collect_once(repository, source, 20, clock=lambda: 100) == "failed"
    assert repository.get_state().last_imported_timestamp == 0
    assert repository.recent_activity() == []
