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

Schema version 1:
- history: generated id, nullable mac, integer timestamp, nullable url. No uniqueness on source fields.
- devices: mac primary key, friendly_name, optional notes.
- collector_state: singleton id, last_imported_timestamp, last_successful_scrape_at,
  last_scrape_row_count, last_error.
- Timestamp/id and MAC/timestamp/id indexes; PRAGMA user_version tracks local migrations.

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

The API is served by the existing web service at the same host/port as the HTML UI.
It uses the same network access policy as the UI and does not start collections.
There is no database migration or change to collector scheduling/checkpointing.

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

To apply code/configuration changes to the web service without restarting collection:
`docker compose up -d --build --no-deps web`.
