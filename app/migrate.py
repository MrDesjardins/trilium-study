from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def run_migrations() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    config = Config(str(repo_root / "alembic.ini"))
    command.upgrade(config, "head")


def main() -> None:
    run_migrations()


if __name__ == "__main__":
    main()
