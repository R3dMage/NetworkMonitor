# Network history

A small Python service for collecting Asuswrt-Merlin web history over SSH.
The router database is opened read-only. Every returned row is retained, including duplicates.

Two Docker Compose services share one application image and a local database volume:

- **web** runs Flask/Waitress continuously.
- **scheduler** runs Supercronic continuously. At each cron boundary it launches
  `network-history collect-once`; that Python process makes one collection pass and exits.
- **history-data** persists SQLite history, device names, collector state, and the collection lock.

There is no Python scheduling loop and no relative scrape interval.

## Setup

1. Copy `.env.example` to `.env`. Set `ROUTER_HOST` and `ROUTER_SSH_USERNAME`.
2. Put the private SSH key in `secrets/router_key`. Configure its matching public key for
   the router's SSH account. Never commit a private key.
3. Put the router's **verified** SSH host-key entry in `secrets/known_hosts`.
   Obtain/verify its fingerprint through a trusted connection or router console before
   trusting the entry. Unknown or changed host keys are rejected.
   Nonstandard ports use the OpenSSH `[hostname]:port` known-hosts format.
4. For an encrypted private key, put its passphrase in `secrets/key_passphrase`
   and set `ROUTER_SSH_KEY_PASSPHRASE_FILE=/run/secrets/key_passphrase`.
5. Make the secret files readable by container UID 10001. On Linux, restrict file
   permissions and grant UID 10001 (or GID 10001) read access. The mount is read-only.
6. Verify the router has a SQLite CLI with `-readonly` and `-json` support.
   Set `ROUTER_SQLITE_BIN=/opt/bin/sqlite3` if needed. No writable fallback is used.
7. Run `docker compose up -d --build`.
8. Open http://localhost:8080 and use **Collect now** to test the connection.

The default publication is localhost only. The UI has no login and is intended for trusted
local use. Change `WEB_BIND_ADDRESS` only if you intend to expose it on your network.
Forms use CSRF tokens, and router text is escaped and displayed without clickable URL links.

## Explore

Open **Explore** in the navigation, or visit [http://localhost:8080/explore](http://localhost:8080/explore).

- **Activity Explorer** searches raw observations by device, exact domain, and local From/To
  dates. The initial view uses the configured API lookback (seven days by default).
  Results run oldest first and retain duplicate observations. Device and domain filters
  apply together; domain matching does not include subdomains automatically.
- **Domain Lookup** shows overall facts and every per-device breakdown for the exact stored
  hostname or URL.
- **Device Summary** shows overall facts, the raw MAC, friendly name, and notes. Continue
  using the Devices page to edit names and notes.

Dropdowns include all known devices, including older ones. Dates are entered and displayed
in the configured `TZ` timezone. From is inclusive and To is exclusive; existing API range
and page-size limits apply. Blank To means now; blank From means the configured lookback
before To. Daylight-saving times that are nonexistent or ambiguous are rejected with guidance
to choose another boundary. Summary views always cover all collected history.

Use **Next** and **Previous** to browse activity without handling cursors. The web process
retains at most 64 viewed activity pages across browser sessions; observations on saved
pages remain as viewed. New imports do not enter an existing search snapshot. Use Search
or Reset to get fresh results. After a restart or when an older page is evicted, a message
asks you to run Search again. No navigation state is written to the history database.

Explore is server-rendered and works without JavaScript. It uses the same query functions as
the read-only API.

## Request Log

Open **Request Log** in the navigation, or visit
[http://localhost:8080/request-log](http://localhost:8080/request-log).
It shows the latest 100 API query operations, newest first. Enter a positive
whole number to show more; the default maximum is 1,000. Invalid or oversized values
produce an explicit error and are never silently clamped. The table shows time, request
path, submitted parameters, status, result count, and duration. Errors appear beneath the
affected call. Times stay on one line, with the configured `TZ` shown above the table.
IDs, sources, methods, operation names, and resolved filters are stored but not displayed;
there are no per-row detail controls.

### Storage and configuration

Operational records live in **/data/request_log.db**, separately from collected history.
The existing Docker volume persists both files; no new service or history migration is
needed. Only the web service initializes/writes this log. Configuration:

- `REQUEST_LOG_PATH=/data/request_log.db`
- `REQUEST_LOG_MAX_DISPLAY=1000` (configurable from 100 through 10,000)

The log store refuses to use the history database, including a symlink/hardlink to it,
and refuses databases containing unrelated tables. A log-storage problem does not stop
the web service. The two databases share disk capacity, so a full volume can affect both.

The log has its own schema version and one table, `request_log`:
`id`, `started_at_ms`, `source`, `operation`, `parameters_json`,
`effective_parameters_json`, `http_method`, `http_path`, `success`, `http_status`,
`result_count`, `duration_ms`, `error_code`, and `error_message`.
Start timestamps are UTC epoch milliseconds. An index on
`(started_at_ms DESC, id DESC)` supports recent reads and future age-based pruning.
IDs are unique within this database; deleting/recreating it restarts the ID sequence.

There is **no automatic retention**. To discard logs later, stop the web service before
removing only the request-log database and any corresponding `-wal`/`-shm` files,
then restart web. Never remove the history volume or history database to clear logs.
The web service creates a new log database on startup.

### What is recorded

| Caller | Source | Operation |
| --- | --- | --- |
| GET /api/devices | http | list_devices |
| GET /api/activity | http | get_activity |
| GET /api/devices/{mac}/activity | http | get_device_activity |
| GET /api/devices/{mac} | http | get_device_summary |
| GET /api/domains/{domain} | http | get_domain_summary |

One record covers parameter validation, the query, and response creation. HTTP status
is the final response status. Duration excludes log persistence and network transmission.
Activity counts are observations returned **on that page**, including duplicates.
Device-list counts are returned devices. Summaries count as one object; a summary's
total observation count is not the result count. Empty lists count as zero; failures
have a null count. Resolved ranges, filters, and page sizes are recorded when available.

All HTML UI interactions are excluded, including Explore searches, pagination, Recent
Activity, device management, and Request Log views. Health checks, static assets, and
collector operations are also excluded. Only the read-only API routes above automatically
record operations.

### Parameters and failure behavior

Submitted API query and path parameters are stored as received by Flask, including
unknown parameters, repeated values, complete URL identifiers, and pagination cursors.
There is no parameter sanitization, redaction, or storage truncation. Percent-encoding
is decoded by the HTTP framework. If a query parameter has the same name as a path
parameter, the path value uses that name and the conflicting query value is retained
under `query_parameters`.

Headers, cookies, SSH credentials/configuration, environment values, response bodies,
returned history rows, and device notes are not collected by the logger. Values a caller
explicitly puts in API parameters are recorded as-is. The inspection page still escapes
HTML and displays parameters as name/value pairs; long values wrap within their column.
Stored parameter values remain intact.

Known validation failures retain their messages. Unexpected exceptions use a generic
description and exception type, without SQL, configuration, or raw exception text.

Logging uses one short insert, SQLite WAL, and 25 ms lock timeouts for both the local
logger lock and SQLite contention. These bound lock waits, not arbitrary operating-system
disk latency. On failure, the query's result/status remains unchanged and that log entry
is dropped. A sanitized warning goes to container logs and a 30-second cooldown limits
repeated failures. A later request retries; there is no worker, queue, or automatic replay.
The Request Log page shows unavailable/degraded state when the logger cannot be read.

### Reusing operation logging

`request_logging.RequestLogger` has no Flask dependency. An internal caller can use
`logger.operation(source="internal", operation="get_activity", parameters=...)` as a
context manager, and pass its query result through `scope.result(...)`.
The scope records selected metadata on completion and records failures while preserving
the original exception. A future MCP adapter can use exactly the same interface with
`source="mcp"`; no MCP integration is implemented here.

HTTP adapters use the same logger through a shared decorator and completion hooks.
Calling a query function directly remains side-effect free unless the caller explicitly
wraps it in an operation scope.

To apply this feature and its new environment settings:
`docker compose up -d --build --no-deps web`.

## Scheduling

`SCRAPE_CRON="*/5 * * * *"` targets minute 00, 05, 10, 15, ... each hour.
Schedule evaluation uses `SCHEDULE_TIMEZONE=UTC` by default. `TZ` controls the UI timezone.
Only five numeric cron fields (digits, ranges, lists, stars, steps) are accepted;
Supercronic additionally validates their ranges.

A scheduler started at 00:03 first launches at 00:05. It does not scrape immediately
on startup. Ordinary OS scheduling latency applies; Docker must be running and the host awake.

Supercronic dispatches on each boundary with its overlapping option enabled.
A shared, nonblocking OS file lock allows only one scrape to actually collect.
Other invocations log a skip and exit immediately, including simultaneous manual runs.
The lock covers checkpoint reads, SSH retrieval, and the local commit. It is not deduplication.
Do not delete the lock file during operation. The OS releases the lock when its process exits.

Missed ticks are not queued. The next invocation fetches everything eligible since the stored
checkpoint, subject to what the router still retains. Collection does not reset the cron schedule.
To apply a changed .env, recreate services with `docker compose up -d`.

## One collection pass

1. Acquire the lock; exit successfully with a skip log if another collection owns it.
2. Read the checkpoint (initially zero) and capture `cutoff = current UTC time - safety delay`.
3. Run a fixed SELECT over SSH for `checkpoint < timestamp <= cutoff`.
4. Require a successful remote exit and valid complete JSON before any local import.
5. In one transaction, insert **every** returned history row, discover new non-null MACs,
   update last-success status, and advance the checkpoint to the greatest imported timestamp.
6. Release resources and exit. Failures exit nonzero and record an error when storage is accessible.

SQL is sent through SSH stdin; the shell command's arguments are POSIX-quoted.
The CLI uses `-init /dev/null -readonly -batch -bail -json`. No router DDL, writes,
journal changes, deletions, or database copies are performed.

An empty successful scrape records zero rows and leaves the checkpoint unchanged.
A failed scrape preserves the previous successful time/count and checkpoint.
A successful scrape clears the previous error.
Database commit failures roll back both imported rows and their checkpoint.

The initial pass imports all retained settled history. The source output is validated in memory;
very large initial histories may need more memory or a future streaming implementation.

**Safety-delay limitation:** rows arriving later with timestamps at or below the committed
checkpoint are missed. There is deliberately no overlap or deduplication.
Timestamps are assumed to be Unix seconds, with synchronized router/host clocks.
History deleted by the router before collection cannot be recovered. Router resets and clock
rollback never automatically reset your checkpoint.

## UI and manual collection

The activity page shows the latest 100 matching records, with timestamp/id descending ordering.
Filter by MAC and manage friendly names/notes on the Devices page. Names never overwrite raw
MACs; URLs/hostnames are kept verbatim. Null source text is preserved and shown as missing.

**Collect now** starts the same short-lived command as a child of the web service, without
blocking HTTP requests. Both services mount SSH secrets for this reason. The web service
reaps manual children and terminates an active child on shutdown. It has no recurring timer.
Refresh to see the result. Names/notes are local; history retention is indefinite.

Useful commands:

- Start: `docker compose up -d --build`
- Stop/remove containers, keep data: `docker compose down`
- Follow all logs: `docker compose logs -f`
- Scheduled collection logs: `docker compose logs -f scheduler`
- Manual UI collection logs: `docker compose logs -f web`
- Collect directly: `docker compose exec scheduler network-history collect-once`
- Collect using a temporary container: `docker compose run --rm --no-deps scheduler collect-once`
- Check status: `docker compose ps`

Each collection logs its trigger, time bounds, row count, duration, and outcome.
No SSH secrets or history URLs are logged. Debug/manual commands share the same execution lock.
`docker compose down -v` **deletes the history volume**; do not use it to perform a routine restart.

## Configuration

All configuration comes from environment variables; Compose uses your local `.env`.
See `.env.example` for the complete list. SSH key authentication is intentionally the only
initial authentication method; there are no built-in passwords, agents, or secret discovery.

- Router: `ROUTER_HOST`, `ROUTER_SSH_PORT`, `ROUTER_SSH_USERNAME`,
  `ROUTER_SSH_KEY_FILE`, `ROUTER_SSH_KEY_PASSPHRASE_FILE`,
  `ROUTER_SSH_KNOWN_HOSTS_FILE`, `ROUTER_DB_PATH`, `ROUTER_SQLITE_BIN`.
- Bounds: `SSH_CONNECT_TIMEOUT_SECONDS` and `SSH_COMMAND_TIMEOUT_SECONDS`.
- Collection: `SCRAPE_CRON`, `SCHEDULE_TIMEZONE`, `SAFETY_DELAY_SECONDS`.
- Storage: `DATABASE_BACKEND=sqlite`, `DATABASE_PATH=/data/network_history.db`.
  `DATABASE_URL` is reserved and must be empty for SQLite.
- Display: `TZ` is an IANA timezone such as `America/New_York`.
- HTTP: `WEB_BIND_ADDRESS` and `WEB_PORT` control the host's published endpoint.
  Compose keeps the container HTTP port at 8080.

Only SQLite is currently implemented. Selecting another backend fails explicitly.
Keep DATABASE_PATH inside /data when using the supplied Compose file.

## Storage and extension points

`storage/repository.py` is a small protocol used by the collector and UI.
`storage/sqlite.py` owns SQL, connections, transactions, schema versioning, and lock selection.
A future PostgreSQL/MySQL adapter implements the same operations and backend-appropriate
collection locking; the collector/UI do not need SQL changes.

SQLite uses WAL and a busy timeout on a Docker-managed **local** volume. Short write
transactions allow web reads while collecting. Do not place this SQLite database on NFS,
SMB, object storage, or a shared cloud filesystem. For remote/cloud storage use a future
database-server adapter. A volume provides persistence, not backups; back up using SQLite's
backup API or stop both services and copy the complete database directory.

Schema version 2 (the original tables are unchanged):
- history: generated id, nullable mac, integer timestamp, nullable url. No uniqueness on source fields.
- devices: mac primary key, friendly_name, optional notes.
- collector_state: singleton id, last_imported_timestamp, last_successful_scrape_at,
  last_scrape_row_count, last_error.
- Timestamp/id, MAC/timestamp/id, and URL/timestamp/id/MAC indexes.
  PRAGMA user_version tracks local migrations; version 2 adds only the domain index.

## Development and checks

Python 3.12+:

```text
python -m venv .venv
python -m pip install -e ".[test]"
python -m pytest
python -m ruff check .
```

Activate the virtual environment before installing. For local development, set
DATABASE_PATH to a local file; the default /data path is intended for Docker.

Docker test image (includes a SQLite CLI for integration tests):

```text
docker build --target test -t network-history:test .
docker run --rm network-history:test
docker run --rm --entrypoint python network-history:test -m ruff check --no-cache /app
```

The tests use synthetic router data; they do not connect to your actual router.
Source files live in this repository. Runtime history lives in the named Docker volume.

## Read-only HTTP API

The API uses **port 8080 by default**, shared with the HTML UI. Its default base URL is
[http://localhost:8080](http://localhost:8080). For example:
[http://localhost:8080/api/devices](http://localhost:8080/api/devices).

`WEB_PORT` in `.env` controls the published host port. `WEB_PORT=8090` puts both the
UI and API at `http://localhost:8090`; the internal container port remains 8080.
`localhost` means the computer running Docker. The default binding, `127.0.0.1`,
permits connections from that computer only. If LAN access has been explicitly enabled,
use the Docker host's address and configured port.
It uses the same network access policy as the UI and does not start collections.
Collector scheduling/checkpointing is unchanged. The activity/summary extension adds one local
index through schema migration 1 -> 2.

### List devices

`GET /api/devices` returns devices with collected activity in the last seven days.
Optional `days` accepts a positive integer or `all`:

```text
GET /api/devices
GET /api/devices?days=30
GET /api/devices?days=all
```

A numeric lookback uses a rolling [from, to) window ending at the current UTC second.
`days=all` returns every known device, including inactive devices, with `range: null`.
Longer device lookbacks return device metadata only, not history rows.
The HTML Devices page continues to show all known devices.

Example (request at 2026-09-16T16:00:00Z):

```json
{
  "range": {
    "from": "2026-09-09T16:00:00Z",
    "to": "2026-09-16T16:00:00Z"
  },
  "devices": [
    {
      "mac": "AA:BB:CC:DD:EE:01",
      "friendly_name": "Office laptop",
      "notes": "Work computer"
    }
  ]
}
```

Unassigned names and absent notes are `null`. MAC values are returned exactly as stored.
Use the returned MAC, URL-encoded when necessary, for device activity requests.

### Device activity

`GET /api/devices/{mac}/activity` accepts `from`, `to`, and `limit` for the first page,
or `cursor` alone for continuation pages.

```text
GET /api/devices/AA:BB:CC:DD:EE:01/activity
GET /api/devices/AA:BB:CC:DD:EE:01/activity?from=2026-09-01T00:00:00Z&to=2026-09-08T00:00:00Z&limit=500
GET /api/devices/AA:BB:CC:DD:EE:01/activity?cursor=<returned-cursor>
```

Time rules:

- Supply ISO-8601 datetimes with seconds and an explicit timezone: `Z` or an offset
  such as `-04:00`. Optional fractional seconds support up to six digits.
- Date-only strings, naive datetimes, invalid calendar dates, and leap seconds are rejected.
- Encode a positive offset's plus sign as `%2B`, or use your HTTP client's query parameter encoder.
- Responses normalize timestamps to UTC with `Z`. UI timezone configuration does not affect API times.
- `from` is inclusive; `to` is exclusive. Adjacent time windows do not duplicate boundary rows.
- Neither bound: now minus the default window through now.
- Only `to`: the default window ending at `to`.
- Only `from`: `from` through now, subject to the maximum range.
- Both bounds: exactly that interval, subject to the maximum range.

Example using a two-row page:

```json
{
  "device": {
    "mac": "AA:BB:CC:DD:EE:01",
    "friendly_name": "Office laptop"
  },
  "range": {
    "from": "2026-09-01T00:00:00Z",
    "to": "2026-09-08T00:00:00Z"
  },
  "activity": [
    {"timestamp": "2026-09-01T09:15:00Z", "domain": "example.com"},
    {"timestamp": "2026-09-01T09:15:00Z", "domain": "example.com"}
  ],
  "pagination": {
    "limit": 2,
    "has_more": true,
    "next_cursor": "<opaque-cursor>"
  }
}
```

`domain` exposes the existing raw router `url` value without transformation.
It may contain a hostname, a full URL, or `null`. Identical rows remain separate entries.

Results are chronological, with the internal row ID breaking timestamp ties.
Follow `next_cursor` until `has_more` is false and `next_cursor` is null.
No rows are silently truncated and no total-count query is performed.

Cursors preserve the device, effective range, page size, last position, and an initial upper
row ID. Imports made after the first page are excluded from that traversal; start a fresh
query to include them. This works with the existing append-only history without holding a
database transaction open between requests. Friendly names reflect current edits.

Treat cursors as opaque. They are validated on every request, bound to the device, and may
be invalidated by stricter API limits. They are pagination state, not authorization tokens.
Do not combine a cursor with `from`, `to`, or `limit`.

### Limits and errors

| Environment variable | Default | Purpose |
| --- | ---: | --- |
| API_DEFAULT_WINDOW_DAYS | 7 | Default window for both endpoints |
| API_MAX_RANGE_DAYS | 31 | Maximum activity window |
| API_DEFAULT_PAGE_SIZE | 500 | Activity rows per page when limit is omitted |
| API_MAX_PAGE_SIZE | 2000 | Maximum allowed activity page size |

Defaults cannot exceed their corresponding maxima. All values must be positive integers.
Limits are rejected rather than silently clamped. Maximum activity duration does not constrain
device-inventory lookbacks; `days=all` never returns history rows.

A known device with no matching rows returns 200 with an empty activity array and no next cursor.
An unknown MAC returns 404. Invalid datetimes, empty/reversed/oversized ranges, invalid limits,
invalid cursors, and unknown or repeated query parameters return 400:

```json
{
  "error": {
    "code": "range_too_large",
    "message": "The activity range cannot exceed 31 days."
  }
}
```

Other query error codes are `invalid_datetime`, `invalid_range`, `invalid_limit`,
`invalid_parameter`, `invalid_cursor`, and `device_not_found`.

### Reusing queries without HTTP

`queries.query_devices(repository, settings, days=...)` and
`queries.query_device_activity(repository, settings, mac, from_time=..., to_time=..., limit=..., cursor=...)`
return ordinary structured dictionaries and raise `QueryError` for invalid requests.
Optional query arguments use the same strings as the HTTP API.
These functions have no Flask dependency and can later be called by purpose-built MCP tools.
`api.py` handles HTTP parameters/JSON/status codes; the existing repository owns all SQL.

### Endpoint overview

| Endpoint | Meaning |
| --- | --- |
| GET /api/activity | Raw observations with optional device/domain/time filters |
| GET /api/devices | Existing recent-device list; supports days=all |
| GET /api/devices/{mac} | Device facts over all collected local history |
| GET /api/domains/{domain} | Domain facts over all collected local history, including per-device facts |
| GET /api/devices/{mac}/activity | Existing device-specific activity response and v1 cursors remain supported |

### General activity

`GET /api/activity` accepts optional `device`, `domain`, `from`, `to`, and `limit`
parameters for the initial request, or `cursor` alone for continuation.
All supplied filters combine with **AND**. `device` is the exact stored MAC address;
there is no opaque device ID or separate `mac` query parameter.

Full examples on the default port:

```text
http://localhost:8080/api/activity
http://localhost:8080/api/activity?device=AA:BB:CC:DD:EE:FF
http://localhost:8080/api/activity?domain=netflix.com
http://localhost:8080/api/activity?device=AA:BB:CC:DD:EE:FF&domain=netflix.com
http://localhost:8080/api/activity?from=2026-09-01T00:00:00Z&to=2026-09-08T00:00:00Z&limit=500
http://localhost:8080/api/activity?cursor=<returned-cursor>
```

The same seven-day default, 31-day maximum, 500-row default page, 2,000-row maximum page,
timezone parsing, UTC responses, and [from, to) bounds apply. Existing API environment
variables control both activity endpoints.

Example response:

```json
{
  "filters": {"device": "AA:BB:CC:DD:EE:FF", "domain": "netflix.com"},
  "range": {"from": "2026-09-10T12:00:00Z", "to": "2026-09-17T12:00:00Z"},
  "activity": [
    {
      "timestamp": "2026-09-16T20:00:00Z",
      "mac": "AA:BB:CC:DD:EE:FF",
      "friendly_name": "Living room TV",
      "domain": "netflix.com"
    },
    {
      "timestamp": "2026-09-16T20:00:00Z",
      "mac": "AA:BB:CC:DD:EE:FF",
      "friendly_name": "Living room TV",
      "domain": "netflix.com"
    }
  ],
  "pagination": {"limit": 500, "has_more": false, "next_cursor": null}
}
```

Omitted filters are echoed as null. Observations remain chronological, with the internal
row ID breaking ties. Duplicates and missing source MAC/domain values are preserved.
Unknown device/domain filters return 200 with an empty activity array.

General activity uses a v2 cursor that records endpoint scope, both filters, the fixed
range/page size, last position, and initial upper row ID. It has the same snapshot behavior
as the existing device-activity endpoint: later imports are excluded until a fresh request.
Cursors cannot be exchanged between endpoints or combined with other parameters.
Malformed/oversized cursors and invalid parameters produce structured 400 responses.
Cursors are limited to 4,096 characters; unusually long raw filter values that cannot fit
produce an explicit error instead of returning an unusable continuation.

### Exact domain identifiers

`domain` refers to the raw stored router URL/hostname value. Matching is exact and
case-sensitive. `netflix.com`, `www.netflix.com`, `Netflix.com`, and a full URL
are separate values. There is no subdomain expansion, suffix matching, normalization,
reputation lookup, or interpretation.

For query parameters use your HTTP client's parameter encoder. For a domain-summary path,
percent-encode the identifier, including reserved characters in full URLs. For example,
the stored `https://example.com/a?x=1#part` has this summary URL:

```text
http://localhost:8080/api/domains/https%3A%2F%2Fexample.com%2Fa%3Fx%3D1%23part
```

### Domain summaries

`GET /api/domains/{domain}` summarizes all matching observations in the local database.
For example: [http://localhost:8080/api/domains/netflix.com](http://localhost:8080/api/domains/netflix.com).

```json
{
  "domain": "netflix.com",
  "scope": "all_collected_history",
  "first_seen": "2026-09-02T18:00:00Z",
  "last_seen": "2026-09-16T20:00:00Z",
  "total_observation_count": 3,
  "distinct_device_count": 2,
  "devices": [
    {
      "mac": "AA:BB:CC:DD:EE:01",
      "friendly_name": "Office laptop",
      "first_seen": "2026-09-02T18:00:00Z",
      "last_seen": "2026-09-02T18:00:00Z",
      "observation_count": 1
    },
    {
      "mac": "AA:BB:CC:DD:EE:FF",
      "friendly_name": "Living room TV",
      "first_seen": "2026-09-16T20:00:00Z",
      "last_seen": "2026-09-16T20:00:00Z",
      "observation_count": 2
    }
  ]
}
```

Counts include every duplicate observation. All matching device groups are returned,
ordered by MAC; no groups are silently truncated. Missing MAC observations contribute
to the total and appear in a final `mac: null` group. `distinct_device_count` counts
non-null MACs only. Names without an assignment are null.

A single grouped read supplies per-device statistics; overall statistics are combined from
those same groups so concurrent imports cannot cause inconsistent totals. An unknown domain
returns 404 with error code `domain_not_found`.

### Device summaries

`GET /api/devices/{mac}` provides overall local-history facts and current device metadata:

```text
http://localhost:8080/api/devices/AA:BB:CC:DD:EE:FF
```

```json
{
  "mac": "AA:BB:CC:DD:EE:FF",
  "friendly_name": "Living room TV",
  "notes": null,
  "scope": "all_collected_history",
  "first_seen": "2026-09-01T10:00:00Z",
  "last_seen": "2026-09-16T20:00:00Z",
  "total_observation_count": 15,
  "distinct_domain_count": 4
}
```

`total_observation_count` includes duplicates and rows with a missing domain.
`distinct_domain_count` counts distinct non-null raw domain values. A known device with no
history returns zero counts and null first/last-seen timestamps. An unknown MAC returns
404 with error code `device_not_found`.

Summary endpoints intentionally accept no query parameters: their scope is overall collected
local history, not the default recent window. They do not contain top-domain lists, threat
scores, anomaly scores, or AI judgments.

### Summary implementation and performance

`queries.query_activity(repository, settings, device=..., domain=..., from_time=..., to_time=..., limit=..., cursor=...)`,
`queries.get_device_summary(repository, mac)`, and
`queries.get_domain_summary(repository, domain)` are reusable without HTTP or Flask.
Controllers only adapt request parameters and JSON/error responses; repository methods own SQL.

The existing timestamp/id and MAC/timestamp/id indexes support general and device activity.
The new URL/timestamp/id/MAC index supports domain-filtered activity and covers the history
fields needed for domain grouping. Exact overall counts still examine matching history:
they are not constant-time counters. Device distinct-domain counts read that device's rows.
There are no reporting tables, background aggregation jobs, or additional dependencies.
