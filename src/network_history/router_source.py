import json
import queue
import select
import shlex
import threading
import time
from pathlib import Path

import paramiko

from network_history.config import Settings
from network_history.models import HistoryRow


class RouterError(RuntimeError):
    pass


def remote_command(settings: Settings) -> str:
    # SSH exec passes a string to a POSIX shell; quote every argument, including paths.
    # -init prevents a router-side .sqliterc from introducing side effects or formatting.
    return shlex.join(
        [
            settings.router_sqlite_bin,
            "-init",
            "/dev/null",
            "-readonly",
            "-batch",
            "-bail",
            "-json",
            settings.router_db_path,
        ]
    )


def history_query(checkpoint: int, cutoff: int) -> str:
    # SQL travels on stdin, never through the remote shell.
    if type(checkpoint) is not int or type(cutoff) is not int:
        raise ValueError("Query bounds must be integers")
    if checkpoint < 0:
        raise ValueError("Checkpoint cannot be negative")
    return (
        "SELECT mac, timestamp, url FROM history "
        f"WHERE timestamp > {checkpoint} AND timestamp <= {cutoff} "
        "ORDER BY timestamp;\n"
    )


def parse_rows(output: bytes, checkpoint: int, cutoff: int) -> list[HistoryRow]:
    try:
        # SQLite emits no bytes for a SELECT with zero rows.
        payload = json.loads(output) if output.strip() else []
        if not isinstance(payload, list):
            raise ValueError("Expected a JSON array")
        result = []
        for item in payload:
            if not isinstance(item, dict) or set(item) != {"mac", "timestamp", "url"}:
                raise ValueError("Unexpected history row shape")
            timestamp = item["timestamp"]
            if type(timestamp) is not int or not checkpoint < timestamp <= cutoff:
                raise ValueError("Timestamp outside the requested window")
            for key in ("mac", "url"):
                if item[key] is not None and not isinstance(item[key], str):
                    raise ValueError(f"Invalid {key} type")
            result.append(HistoryRow(**item))
        return result
    except (ValueError, UnicodeError) as exc:
        raise RouterError("Invalid or incomplete history output; no rows imported") from exc


def read_channel(channel, timeout: int) -> tuple[bytes, bytes, int]:
    """Drain both SSH streams before exit-status retrieval to avoid window deadlocks."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    errors = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RouterError("Router SQLite command timed out")
        if channel.recv_ready():
            output.extend(channel.recv(65536))
        if channel.recv_stderr_ready():
            chunk = channel.recv_stderr(65536)
            errors.extend(chunk[: max(0, 65536 - len(errors))])
        # Wait for EOF as well as the status: a status can precede the last output packet.
        if (channel.eof_received or channel.closed) and not (
            channel.recv_ready() or channel.recv_stderr_ready()
        ):
            # EOF may precede exit status. recv_exit_status has no timeout itself,
            # so wait for that network event in a bounded helper, never accept partial output.
            if channel.exit_status_ready():
                status = channel.recv_exit_status()
            else:
                statuses = queue.Queue(maxsize=1)

                def receive_status(results):
                    try:
                        results.put(channel.recv_exit_status())
                    except Exception as exc:
                        results.put(exc)

                threading.Thread(target=receive_status, args=(statuses,), daemon=True).start()
                try:
                    status = statuses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise RouterError("Router exit status timed out") from exc
                if isinstance(status, Exception):
                    raise RouterError("Could not read router exit status") from status
            return bytes(output), bytes(errors), status
        if not channel.recv_ready() and not channel.recv_stderr_ready():
            select.select([channel], [], [], min(remaining, 1.0))


class RouterSource:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, checkpoint: int, cutoff: int) -> list[HistoryRow]:
        settings = self.settings
        settings.validate_router()
        query = history_query(checkpoint, cutoff)
        passphrase = None
        if settings.router_ssh_key_passphrase_file:
            passphrase = Path(settings.router_ssh_key_passphrase_file).read_text().rstrip("\r\n")
        with paramiko.SSHClient() as client:
            client.load_host_keys(settings.router_ssh_known_hosts_file)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                client.connect(
                    settings.router_host,
                    port=settings.router_ssh_port,
                    username=settings.router_ssh_username,
                    key_filename=settings.router_ssh_key_file,
                    passphrase=passphrase,
                    allow_agent=False,
                    look_for_keys=False,
                    timeout=settings.ssh_connect_timeout_seconds,
                    banner_timeout=settings.ssh_connect_timeout_seconds,
                    auth_timeout=settings.ssh_connect_timeout_seconds,
                    channel_timeout=settings.ssh_connect_timeout_seconds,
                )
                stdin, stdout, stderr = client.exec_command(
                    remote_command(settings),
                    timeout=settings.ssh_command_timeout_seconds,
                    get_pty=False,
                )
                try:
                    stdin.write(query)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                    output, errors, status = read_channel(
                        stdout.channel, settings.ssh_command_timeout_seconds
                    )
                finally:
                    stdin.close()
                    stdout.close()
                    stderr.close()
                if status != 0:
                    detail = errors.decode("utf-8", errors="replace").strip()
                    raise RouterError(f"Router SQLite exited with status {status}: {detail}")
                if errors.strip():
                    # Conservatively fail on stderr rather than accept an ambiguous partial result.
                    raise RouterError(
                        "Router SQLite reported warnings/errors; inspect router setup"
                    )
                return parse_rows(output, checkpoint, cutoff)
            except (paramiko.SSHException, OSError) as exc:
                raise RouterError(f"SSH failed ({type(exc).__name__}): {exc}") from exc
