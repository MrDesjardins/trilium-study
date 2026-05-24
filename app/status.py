from __future__ import annotations

import importlib.util
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Job, Lesson
from app.pipeline import STAGES, cleaned_source_material, estimate_narration_minutes, minimum_script_words, script_word_count


STAGE_LABELS = {
    "collect": "Collect notes",
    "normalize": "Normalize content",
    "script": "Generate script",
    "flashcards": "Create flashcards",
    "audio": "Synthesize narration",
    "video": "Render MP4",
    "upload": "Upload to YouTube",
}

BASE_STAGE_ESTIMATES_SECONDS = {
    "collect": 8,
    "normalize": 3,
    "script": 20,
    "flashcards": 10,
    "audio": 45,
    "video": 35,
    "upload": 50,
}

STAGE_STATE_DISPLAY = {
    "completed": "☑️ Done",
    "queued": "⏳ Queued",
    "running": "⏳ Running",
    "pending": "⏳ Pending",
    "failed": "⚠️ Failed",
    "skipped": "☑️ Reused",
}


@dataclass
class RuntimeCheck:
    key: str
    label: str
    ok: bool
    detail: str


def total_estimated_seconds() -> int:
    return sum(BASE_STAGE_ESTIMATES_SECONDS[stage] for stage in STAGES)


def stage_index(stage: str | None) -> int:
    if stage not in STAGES:
        return 0
    return STAGES.index(stage)


def estimate_remaining_seconds(stage: str | None, stage_state: str) -> int:
    return estimate_remaining_seconds_with_estimates(stage, stage_state, BASE_STAGE_ESTIMATES_SECONDS)


def estimate_remaining_seconds_with_estimates(stage: str | None, stage_state: str, stage_estimates_seconds: dict[str, int]) -> int:
    if stage_state == "completed":
        return 0
    if stage_state == "queued":
        return sum(stage_estimates_seconds[name] for name in STAGES)
    index = stage_index(stage)
    remaining = 0
    for idx, name in enumerate(STAGES):
        if idx < index:
            continue
        if idx == index and stage_state in {"running", "pending"}:
            remaining += math.ceil(stage_estimates_seconds[name] * 0.7)
        else:
            remaining += stage_estimates_seconds[name]
    return remaining


def format_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "done"
    minutes, secs = divmod(seconds, 60)
    if minutes == 0:
        return f"{secs}s"
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_duration_seconds(seconds: float | int | None) -> str:
    if seconds is None or seconds <= 0:
        return "unknown"
    return format_seconds(round(seconds))


def progress_percent(stage: str | None, stage_state: str) -> int:
    if stage_state == "completed":
        return 100
    if stage_state == "queued":
        return 3
    if stage_state == "failed":
        return max(5, stage_index(stage) * 100 // len(STAGES))
    base = stage_index(stage) / len(STAGES)
    current = 0.6 / len(STAGES) if stage_state == "running" else 0.25 / len(STAGES)
    return max(1, min(99, round((base + current) * 100)))


def stage_state_display(stage_state: str) -> str:
    return STAGE_STATE_DISPLAY.get(stage_state, stage_state.replace("_", " ").title())


def queue_positions(session: Session) -> dict[int, int]:
    jobs = session.scalars(
        select(Job).where(Job.job_type == "lesson_pipeline", Job.state.in_(["pending", "running"])).order_by(Job.created_at.asc(), Job.id.asc())
    ).all()
    positions: dict[int, int] = {}
    pending_counter = 0
    running_seen = False
    for job in jobs:
        if job.state == "running" and not running_seen:
            positions[job.id] = 0
            running_seen = True
        else:
            pending_counter += 1
            positions[job.id] = pending_counter
    return positions


def _script_metadata(lesson: Lesson) -> dict:
    if not lesson.script_provenance:
        return {}
    try:
        return json.loads(lesson.script_provenance)
    except json.JSONDecodeError:
        return {}


def _normalized_word_count(lesson: Lesson, script_metadata: dict) -> int | None:
    count = script_metadata.get("normalized_word_count")
    if isinstance(count, int) and count > 0:
        return count
    if lesson.normalized_content_path:
        path = Path(lesson.normalized_content_path)
        if path.exists():
            return script_word_count(cleaned_source_material(path.read_text(encoding="utf-8")))
    return None


def lesson_stage_estimates_seconds(lesson: Lesson) -> dict[str, int]:
    script_metadata = _script_metadata(lesson)
    normalized_word_count = _normalized_word_count(lesson, script_metadata)
    script_word_total = script_metadata.get("script_word_count")
    if not isinstance(script_word_total, int) or script_word_total <= 0:
        if normalized_word_count:
            script_word_total = minimum_script_words(normalized_word_count)
        else:
            script_word_total = 500
    estimated_narration_seconds = max(45, round(estimate_narration_minutes(script_word_total) * 60))
    normalized_words = normalized_word_count or script_word_total
    return {
        "collect": max(8, min(60, round(normalized_words / 900))),
        "normalize": max(3, min(45, round(normalized_words / 500))),
        "script": max(20, min(360, round(normalized_words / 35))),
        "flashcards": max(10, min(120, round(script_word_total / 30))),
        "audio": estimated_narration_seconds,
        "video": max(35, round(estimated_narration_seconds * 0.35)),
        "upload": max(50, round(estimated_narration_seconds * 0.18)),
    }


def _artifact_metadata(lesson: Lesson, artifact_type: str) -> dict:
    for artifact in lesson.artifacts:
        if artifact.artifact_type != artifact_type or not artifact.metadata_json:
            continue
        try:
            return json.loads(artifact.metadata_json)
        except json.JSONDecodeError:
            return {}
    return {}


def _actual_media_duration_seconds(lesson: Lesson) -> float | None:
    for artifact_type in ("video", "audio"):
        metadata = _artifact_metadata(lesson, artifact_type)
        duration_seconds = metadata.get("duration_seconds")
        if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
            return float(duration_seconds)
    return None


def lesson_status_payload(session: Session, lesson: Lesson, queue_map: dict[int, int]) -> dict:
    job = session.get(Job, lesson.current_job_id) if lesson.current_job_id else None
    queue_position = queue_map.get(job.id) if job else None
    current_stage = job.current_stage if job and job.current_stage else lesson.stage
    current_state = lesson.stage_state
    if job and job.state == "pending":
        current_state = "queued"
    elif job and job.state == "running":
        current_state = "running"
    elif job and job.state == "failed":
        current_state = "failed"
    stage_estimates_seconds = lesson_stage_estimates_seconds(lesson)
    eta_seconds = estimate_remaining_seconds_with_estimates(current_stage, current_state, stage_estimates_seconds)
    eta_seconds += (queue_position or 0) * sum(stage_estimates_seconds[name] for name in STAGES)

    steps = []
    current_idx = stage_index(current_stage)
    for idx, stage in enumerate(STAGES):
        if current_state == "failed" and idx == current_idx:
            state = "failed"
        elif idx < current_idx:
            state = "completed"
        elif idx == current_idx:
            state = current_state
        else:
            state = "pending"
        if current_state == "completed":
            state = "completed"
        steps.append({"key": stage, "label": STAGE_LABELS[stage], "state": state, "display_state": stage_state_display(state)})

    script_metadata = _script_metadata(lesson)
    actual_duration_seconds = _actual_media_duration_seconds(lesson)
    eta_label = "ETA"
    eta_text = format_seconds(eta_seconds)
    if current_state == "completed" and actual_duration_seconds:
        eta_label = "Runtime"
        eta_text = format_duration_seconds(actual_duration_seconds)

    return {
        "id": lesson.id,
        "title": lesson.title,
        "stage": current_stage,
        "stage_label": STAGE_LABELS.get(current_stage or "", "Waiting"),
        "stage_state": current_state,
        "stage_state_display": stage_state_display(current_state),
        "stage_error": lesson.stage_error,
        "progress_percent": progress_percent(current_stage, current_state),
        "queue_position": queue_position,
        "eta_label": eta_label,
        "eta_text": eta_text,
        "actual_duration_seconds": actual_duration_seconds,
        "steps": steps,
        "audio_path": lesson.audio_path,
        "video_path": lesson.video_path,
        "youtube_url": lesson.youtube_upload.video_url if lesson.youtube_upload else None,
        "script_word_count": script_metadata.get("script_word_count"),
        "normalized_word_count": script_metadata.get("normalized_word_count"),
        "script_minimum_words": script_metadata.get("script_minimum_words"),
        "estimated_narration_minutes": script_metadata.get("estimated_narration_minutes"),
        "target_duration_minutes": script_metadata.get("target_duration_minutes"),
    }


def runtime_checks(settings: Settings) -> list[RuntimeCheck]:
    return [
        RuntimeCheck("ffmpeg", "ffmpeg", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "not installed"),
        RuntimeCheck("espeak_ng", "espeak-ng", shutil.which("espeak-ng") is not None, shutil.which("espeak-ng") or "not installed"),
        RuntimeCheck("numpy", "numpy", importlib.util.find_spec("numpy") is not None, "python package"),
        RuntimeCheck("soundfile", "soundfile", importlib.util.find_spec("soundfile") is not None, "python package"),
        RuntimeCheck("kokoro", "kokoro", importlib.util.find_spec("kokoro") is not None, "python package"),
        RuntimeCheck("youtube_token", "YouTube token", Path(settings.youtube_token_file).expanduser().exists(), settings.youtube_token_file),
        RuntimeCheck("openai_key", "OpenAI key", bool(settings.openai_api_key), "configured" if settings.openai_api_key else "missing"),
    ]
