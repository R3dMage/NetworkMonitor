import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    pass


def _integer(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigurationError(f"{name} is outside its allowed range")
    return value


@dataclass(frozen=True)
class Settings:
    database_backend: str = "sqlite"
    database_path: str = "/data/network_history.db"
    database_url: str = ""
    timezone: str = "America/New_York"
    schedule_timezone: str = "UTC"
    scrape_cron: str = "*/5 * * * *"
    safety_delay_seconds: int = 300
    router_host: str = ""
    router_ssh_port: int = 22
    router_ssh_username: str = ""
    router_ssh_key_file: str = "/run/secrets/router_key"
    router_ssh_key_passphrase_file: str = ""
    router_ssh_known_hosts_file: str = "/run/secrets/known_hosts"
    router_db_path: str = "/jffs/.sys/WebHistory/WebHistory.db"
    router_sqlite_bin: str = "sqlite3"
    ssh_connect_timeout_seconds: int = 15
    ssh_command_timeout_seconds: int = 120
    web_port: int = 8080
    api_default_window_days: int = 7
    api_max_range_days: int = 31
    api_default_page_size: int = 500
    api_max_page_size: int = 2000

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for name in (
            "database_backend",
            "database_path",
            "database_url",
            "timezone",
            "schedule_timezone",
            "scrape_cron",
            "router_host",
            "router_ssh_username",
            "router_ssh_key_file",
            "router_ssh_key_passphrase_file",
            "router_ssh_known_hosts_file",
            "router_db_path",
            "router_sqlite_bin",
        ):
            env_name = "TZ" if name == "timezone" else name.upper()
            if env_name in os.environ:
                values[name] = os.environ[env_name]
        values.update(
            safety_delay_seconds=_integer("SAFETY_DELAY_SECONDS", 300, minimum=0),
            router_ssh_port=_integer("ROUTER_SSH_PORT", 22, maximum=65535),
            ssh_connect_timeout_seconds=_integer("SSH_CONNECT_TIMEOUT_SECONDS", 15),
            ssh_command_timeout_seconds=_integer("SSH_COMMAND_TIMEOUT_SECONDS", 120),
            web_port=_integer("WEB_PORT", 8080, maximum=65535),
            api_default_window_days=_integer("API_DEFAULT_WINDOW_DAYS", 7),
            api_max_range_days=_integer("API_MAX_RANGE_DAYS", 31),
            api_default_page_size=_integer("API_DEFAULT_PAGE_SIZE", 500),
            api_max_page_size=_integer("API_MAX_PAGE_SIZE", 2000),
        )
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.api_default_window_days <= self.api_max_range_days <= 3652058:
            raise ConfigurationError(
                "API day limits must be positive; default cannot exceed maximum"
            )
        if not 1 <= self.api_default_page_size <= self.api_max_page_size < 2**63 - 1:
            raise ConfigurationError(
                "API page limits must be positive; default cannot exceed maximum"
            )
        if self.database_backend != "sqlite":
            raise ConfigurationError(
                f"Unsupported DATABASE_BACKEND: {self.database_backend!r}; currently sqlite only"
            )
        if self.database_url:
            raise ConfigurationError("Use DATABASE_PATH for SQLite; DATABASE_URL is reserved")
        if not self.database_path or self.database_path == ":memory:":
            raise ConfigurationError("DATABASE_PATH must name a persistent database file")
        for name in (self.timezone, self.schedule_timezone):
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigurationError(f"Unknown IANA timezone: {name!r}") from exc

    def validate_router(self) -> None:
        if not self.router_host or not self.router_ssh_username:
            raise ConfigurationError("ROUTER_HOST and ROUTER_SSH_USERNAME must be configured")
        if not self.router_db_path.startswith("/") or "\x00" in self.router_db_path:
            raise ConfigurationError("ROUTER_DB_PATH must be an absolute router path")
        if not self.router_sqlite_bin or "\x00" in self.router_sqlite_bin:
            raise ConfigurationError("ROUTER_SQLITE_BIN must name the router SQLite executable")
        for name, path in (
            ("ROUTER_SSH_KEY_FILE", self.router_ssh_key_file),
            ("ROUTER_SSH_KNOWN_HOSTS_FILE", self.router_ssh_known_hosts_file),
        ):
            if not path or not Path(path).is_file():
                raise ConfigurationError(f"{name} must point to a readable mounted file")
