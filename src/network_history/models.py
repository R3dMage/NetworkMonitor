from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryRow:
    mac: str | None
    timestamp: int
    url: str | None


@dataclass(frozen=True)
class Activity:
    id: int
    mac: str | None
    timestamp: int
    url: str | None
    friendly_name: str | None


@dataclass(frozen=True)
class Device:
    mac: str
    friendly_name: str
    notes: str | None


@dataclass(frozen=True)
class CollectorState:
    last_imported_timestamp: int = 0
    last_successful_scrape_at: int | None = None
    last_scrape_row_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class StoredActivity:
    id: int
    timestamp: int
    url: str | None
    mac: str | None = None
    friendly_name: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    rows: list[StoredActivity]
    snapshot_id: int


@dataclass(frozen=True)
class DeviceSummary:
    mac: str
    friendly_name: str
    notes: str | None
    first_seen: int | None
    last_seen: int | None
    total_observation_count: int
    distinct_domain_count: int


@dataclass(frozen=True)
class DomainDeviceSummary:
    mac: str | None
    friendly_name: str | None
    first_seen: int
    last_seen: int
    observation_count: int
