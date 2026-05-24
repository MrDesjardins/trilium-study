from __future__ import annotations

import hashlib
from pathlib import Path

from app.models import Course, Lesson


def slugify(value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(filter(None, safe.split("-"))) or "item"


def lesson_workspace(root: Path, course: Course, lesson: Lesson) -> Path:
    name = f"{course.trilium_note_id}-{lesson.trilium_note_id}-{slugify(lesson.title)}"
    path = root / slugify(course.title) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(root: Path, course: Course, lesson: Lesson, stage: str, filename: str) -> Path:
    return lesson_workspace(root, course, lesson) / stage / filename


def hashed_filename(prefix: str, content: str, suffix: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}.{suffix.lstrip('.')}"
