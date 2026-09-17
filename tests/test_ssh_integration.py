"""A real SSH connection and SQLite CLI against synthetic router data only."""

import hashlib
import shutil
import socket
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import replace

import paramiko
import pytest

from network_history.collector import collect_once
from network_history.config import Settings
from network_history.router_source import RouterError, RouterSource, remote_command

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None, reason="SQLite CLI needed (included in Docker test image)"
)


class TestSSHServer(paramiko.ServerInterface):
    __test__ = False

    def __init__(self, public_key):
        self.public_key = public_key
        self.ready = threading.Event()
        self.command = None

    def check_auth_publickey(self, username, key):
        if username == "collector" and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode()
        self.ready.set()
        return True


@contextmanager
def router(tmp_path, rows, *, exit_failure=False, trust_key=True):
    # Deliberately hostile filename exercises the actual remote /bin/sh quoting path.
    database = tmp_path / "router'; touch INJECTION_SENTINEL; #.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE history(mac TEXT, timestamp UNSIGNED BIG INT, url TEXT)")
        db.executemany("INSERT INTO history VALUES (?, ?, ?)", rows)
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    host_key = paramiko.RSAKey.generate(2048)
    client_key = paramiko.RSAKey.generate(2048)
    private_key = tmp_path / "client_key"
    client_key.write_private_key_file(str(private_key))
    known_hosts = tmp_path / "known_hosts"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    known_hosts.write_text(
        f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n" if trust_key else ""
    )
    settings = replace(
        Settings(),
        router_host="127.0.0.1",
        router_ssh_port=port,
        router_ssh_username="collector",
        router_ssh_key_file=str(private_key),
        router_ssh_known_hosts_file=str(known_hosts),
        router_db_path=str(database),
        router_sqlite_bin=shutil.which("sqlite3"),
        ssh_connect_timeout_seconds=5,
        ssh_command_timeout_seconds=5,
    )
    server = TestSSHServer(client_key)
    complete = threading.Event()
    failures = []
    transports = []

    def serve():
        try:
            connection, _ = listener.accept()
            with paramiko.Transport(connection) as transport:
                transports.append(transport)
                transport.add_server_key(host_key)
                transport.start_server(server=server)
                channel = transport.accept(timeout=5)
                if channel is None:
                    return  # Expected when the client rejects our unknown host key.
                if not server.ready.wait(5):
                    raise RuntimeError("No exec request")
                query = bytearray()
                while chunk := channel.recv(65536):
                    query.extend(chunk)
                result = subprocess.run(
                    server.command,
                    shell=True,
                    input=bytes(query),
                    capture_output=True,
                    timeout=5,
                    cwd=tmp_path,
                )
                channel.sendall(result.stdout)
                channel.sendall_stderr(result.stderr)
                if exit_failure:
                    channel.sendall_stderr(b"synthetic command failure")
                channel.send_exit_status(1 if exit_failure else result.returncode)
                channel.shutdown_write()
                complete.wait(5)
                channel.close()
        except (EOFError, OSError, paramiko.SSHException):
            if trust_key:
                failures.append("Unexpected SSH transport failure")
        except Exception as exc:
            failures.append(repr(exc))

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield settings, server
    finally:
        complete.set()
        for transport in transports:
            transport.close()
        listener.close()
        worker.join(timeout=6)
        assert not worker.is_alive()
        assert not failures, failures
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash
        assert not (tmp_path / "INJECTION_SENTINEL").exists()


def test_real_ssh_readonly_sqlite_preserves_duplicates_and_boundaries(tmp_path, repository):
    with router(
        tmp_path,
        [
            ("aa", 10, "same.example"),
            ("aa", 10, "same.example"),
            ("bb", 20, 'quoted "url"\nline'),
            ("bb", 21, "unsettled"),
        ],
    ) as (settings, server):
        assert collect_once(repository, RouterSource(settings), 10, clock=lambda: 30) == "success"
        assert server.command == remote_command(settings)
    assert repository.get_state().last_imported_timestamp == 20
    assert repository.get_state().last_scrape_row_count == 3
    assert [r.url for r in repository.recent_activity()].count("same.example") == 2


def test_nonzero_exit_with_valid_partial_output_imports_nothing(tmp_path, repository):
    with router(tmp_path, [("aa", 10, "x")], exit_failure=True) as (settings, _):
        assert collect_once(repository, RouterSource(settings), 0, clock=lambda: 20) == "failed"
    assert repository.recent_activity() == []
    assert repository.get_state().last_imported_timestamp == 0
    assert "status 1" in repository.get_state().last_error


def test_unknown_host_key_is_rejected_before_command_execution(tmp_path):
    with router(tmp_path, [], trust_key=False) as (settings, server):
        with pytest.raises(RouterError, match="SSH failed"):
            RouterSource(settings).fetch(0, 20)
        assert server.command is None


def test_real_sqlite_empty_output_is_successful(tmp_path, repository):
    with router(tmp_path, []) as (settings, _):
        assert collect_once(repository, RouterSource(settings), 0, clock=lambda: 20) == "success"
    assert repository.get_state().last_successful_scrape_at == 20
    assert repository.get_state().last_imported_timestamp == 0
