from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline import DefaultScriptGenerator, FfmpegVideoRenderer, default_kokoro_command, schedule_flashcard_review
from app.status import estimate_remaining_seconds, progress_percent, stage_state_display


class DummySettings:
    openai_api_key = None
    openai_model = "test-model"


def test_script_prompt_marks_added_context_policy():
    generator = DefaultScriptGenerator(DummySettings())
    system, user = generator.build_prompt("Atoms", "source body")

    assert "factual validator" in system
    assert "[Added context]" in system
    assert "Atoms" in user
    assert "validation_summary" in user
    assert "corrected_or_clarified_points" in user


def test_script_generation_requires_openai_key():
    generator = DefaultScriptGenerator(DummySettings())

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        generator.generate("Atoms", "source body")


class DummyFlashcard:
    def __init__(self):
        self.ease_factor = 2.5
        self.repetitions = 0
        self.interval_days = 0
        self.due_at = datetime(2026, 5, 23, tzinfo=timezone.utc)


def test_schedule_flashcard_review_again_resets_repetition():
    flashcard = DummyFlashcard()

    review = schedule_flashcard_review(flashcard, "again", datetime(2026, 5, 23, tzinfo=timezone.utc))

    assert flashcard.repetitions == 0
    assert flashcard.interval_days == 1
    assert review.interval_days_after == 1


def test_schedule_flashcard_review_pass_grows_interval():
    flashcard = DummyFlashcard()

    first = schedule_flashcard_review(flashcard, "pass", datetime(2026, 5, 23, tzinfo=timezone.utc))
    second = schedule_flashcard_review(flashcard, "pass", datetime(2026, 5, 24, tzinfo=timezone.utc))

    assert first.repetitions_after == 1
    assert second.repetitions_after == 2
    assert flashcard.interval_days == 3


def test_default_kokoro_command_targets_repo_module():
    command = default_kokoro_command()

    assert "-m app.kokoro_cli" in command
    assert "{input}" in command
    assert "{output}" in command


def test_progress_helpers_cover_queued_and_completed_states():
    assert progress_percent("collect", "queued") > 0
    assert progress_percent("upload", "completed") == 100
    assert estimate_remaining_seconds("script", "running") > 0
    assert stage_state_display("completed") == "☑️ Done"
    assert stage_state_display("queued") == "⏳ Queued"


class DummyVideoSettings:
    video_background = "#f2e7cc"
    video_width = 1280
    video_height = 720


class DummyLesson:
    title = "What is Philosophy?"


def test_ffmpeg_renderer_matches_audio_duration(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []

    def fake_run(command, check, capture_output=False, text=False):
        calls.append(command)
        if command[0] == "ffprobe":
            assert capture_output is True
            assert text is True
            return SimpleNamespace(stdout="12.5\n", stderr="")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr("app.pipeline.subprocess.run", fake_run)

    renderer = FfmpegVideoRenderer(DummyVideoSettings())
    output = renderer.render(DummyLesson(), tmp_path / "audio.wav", tmp_path / "video.mp4")

    assert calls[0][0] == "ffprobe"
    assert calls[1][0] == "ffmpeg"
    assert ":d=12.500" in calls[1][5]
    assert output["duration_seconds"] == 12.5
