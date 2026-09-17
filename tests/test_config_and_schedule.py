import os
import subprocess
from unittest.mock import patch

import pytest

from network_history.config import ConfigurationError, Settings
from network_history.entrypoint import cron_line, start_scheduler


@pytest.mark.parametrize(
    "name,value",
    [
        ("SAFETY_DELAY_SECONDS", "-1"),
        ("SSH_COMMAND_TIMEOUT_SECONDS", "0"),
        ("ROUTER_SSH_PORT", "99999"),
        ("DATABASE_BACKEND", "postgresql"),
        ("DATABASE_PATH", ":memory:"),
        ("DATABASE_URL", "postgresql://example"),
        ("TZ", "invalid/timezone"),
    ],
)
def test_invalid_settings_fail_explicitly(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.from_env()


@pytest.mark.parametrize(
    "schedule",
    [
        "* * * * *\n* * * * * touch /tmp/injected",
        "* * * * *; touch /tmp/injected",
        "@reboot",
        "* * * * * echo bad",
    ],
)
def test_cron_cannot_inject_a_command(schedule):
    with pytest.raises(ConfigurationError):
        cron_line(schedule)


def test_scheduler_execs_supercronic_and_uses_schedule_timezone():
    settings = Settings(scrape_cron="*/5 * * * *", schedule_timezone="UTC")
    with (
        patch("network_history.entrypoint.Path.write_text") as write,
        patch("network_history.entrypoint.subprocess.run") as validate,
        patch("network_history.entrypoint.os.execvpe") as execute,
    ):
        start_scheduler(settings)
    assert write.call_args.args[0] == (
        "*/5 * * * * network-history collect-once --trigger scheduled\n"
    )
    assert validate.call_args.args[0][1] == "-test"
    assert execute.call_args.args[0] == "supercronic"
    assert "-overlapping" in execute.call_args.args[1]
    assert execute.call_args.args[2]["TZ"] == "UTC"


def test_collect_command_exits_nonzero_and_records_missing_credentials(repository):
    import sys

    environment = dict(
        os.environ, DATABASE_PATH=str(repository.path), ROUTER_HOST="", ROUTER_SSH_USERNAME=""
    )
    result = subprocess.run(
        [sys.executable, "-m", "network_history", "collect-once"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "ROUTER_HOST" in repository.get_state().last_error
