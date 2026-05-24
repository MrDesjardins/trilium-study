from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "name": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
        }
        if hasattr(record, "extra_data"):
            payload["extra"] = record.extra_data
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(settings: Settings) -> None:
    settings.log_path.mkdir(parents=True, exist_ok=True)
    json_path = settings.log_path / settings.json_log_file
    text_path = settings.log_path / settings.text_log_file

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    text_handler = logging.FileHandler(text_path)
    text_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    json_handler = logging.FileHandler(json_path)
    json_handler.setFormatter(JsonFormatter())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))

    root.addHandler(text_handler)
    root.addHandler(json_handler)
    root.addHandler(console)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
