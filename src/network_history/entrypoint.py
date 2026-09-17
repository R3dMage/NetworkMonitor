import os
import re
import subprocess
from pathlib import Path

from network_history.config import ConfigurationError, Settings


def cron_line(schedule: str) -> str:
    # Accept five numeric cron fields only. Never allow a schedule to inject a command.
    fields = schedule.split()
    if len(fields) != 5 or any(not re.fullmatch(r"[0-9*/,\-]+", part) for part in fields):
        raise ConfigurationError("SCRAPE_CRON must contain five numeric cron fields")
    return " ".join(fields) + " network-history collect-once --trigger scheduled\n"


def start_scheduler(settings: Settings) -> None:
    """Generate/validate configuration, then replace Python with Supercronic."""
    cron_path = Path("/tmp/network-history.crontab")
    cron_path.write_text(cron_line(settings.scrape_cron), encoding="utf-8")
    environment = dict(os.environ, TZ=settings.schedule_timezone)
    # These are configuration/exec steps, not a Python scheduling loop.
    subprocess.run(["supercronic", "-test", str(cron_path)], check=True, env=environment)
    os.execvpe(
        "supercronic",
        ["supercronic", "-overlapping", "-split-logs", str(cron_path)],
        environment,
    )
