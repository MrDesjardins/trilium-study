from __future__ import annotations

from app.config import Settings, get_settings
from app.migrate import run_migrations


def bootstrap_database(settings: Settings) -> None:
    run_migrations()


def main() -> None:
    bootstrap_database(get_settings())


if __name__ == "__main__":
    main()
