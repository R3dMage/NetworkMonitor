import argparse
import logging
import signal

from network_history.config import Settings
from network_history.storage import create_repository


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def main() -> int:
    parser = argparse.ArgumentParser(description="Network history service")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="Run the web UI")
    collect = subcommands.add_parser("collect-once", help="Collect once, then exit")
    collect.add_argument("--trigger", choices=("manual", "web", "scheduled"), default="manual")
    subcommands.add_parser("scheduler", help="Exec Supercronic with wall-clock scheduling")
    subcommands.add_parser("init-db", help="Initialize or validate the local database")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = Settings.from_env()
        if args.command == "scheduler":
            from network_history.entrypoint import start_scheduler

            start_scheduler(settings)
            return 0
        signal.signal(signal.SIGTERM, _terminate)
        repository = create_repository(settings)
        repository.initialize()
        if args.command == "init-db":
            return 0
        if args.command == "collect-once":
            from network_history.collector import collect_once
            from network_history.router_source import RouterSource

            result = collect_once(
                repository, RouterSource(settings), settings.safety_delay_seconds, args.trigger
            )
            return 1 if result == "failed" else 0
        from waitress import serve

        from network_history.manual import ManualCollector
        from network_history.web import create_app

        manual = ManualCollector()
        try:
            serve(
                create_app(settings, repository, manual),
                host="0.0.0.0",
                port=settings.web_port,
                threads=4,
            )
        finally:
            manual.close()
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
