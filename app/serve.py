from __future__ import annotations

import argparse

import uvicorn

from app.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Trilium Study web server using .env configuration.")
    parser.add_argument("--reload", action="store_true", help="Enable Uvicorn reload mode.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
