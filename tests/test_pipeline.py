from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Course, Flashcard, Lesson
from app.pipeline import (
    CommandTTSGenerator,
    DefaultFlashcardGenerator,
    DefaultScriptGenerator,
    FfmpegVideoRenderer,
    GeneratedFlashcard,
    cleaned_source_material,
    configure_tts_runtime_warnings,
    default_kokoro_command,
    estimate_narration_minutes,
    media_duration_seconds,
    minimum_script_words,
    narration_markup_issues,
    script_meets_length_target,
    script_word_count,
    sync_flashcards,
    target_duration_minutes,
)
from app.status import estimate_remaining_seconds, lesson_stage_estimates_seconds, lesson_status_payload, progress_percent, stage_state_display


class DummySettings:
    openai_api_key = None
    openai_model = "test-model"
    kokoro_command = "kokoro-cli --input {input} --output {output}"
    kokoro_voice = "af"
    kokoro_lang_code = "en-us"
    kokoro_speed = 1.0


def test_script_prompt_marks_added_context_policy():
    generator = DefaultScriptGenerator(DummySettings())
    system, user = generator.build_prompt("Atoms", "source body")

    assert "factual validator" in system
    assert "structured added-context fields" in system
    assert "Atoms" in user
    assert "validation_summary" in user
    assert "corrected_or_clarified_points" in user
    assert "Do not compress a content-rich lesson" in system
    assert "natural narration only" in user
    assert "no Markdown" in user
    assert "asterisk, dash, hash, greater-than, backtick, or vertical bar" in user
    assert "list it in added_context_notes" in user
    assert "produce enough explanation to comfortably study from the notes" in user
    assert "use examples, analogies, comparisons, and short restatements generously" in system
    assert "optimize for study quality, comprehension, and review value rather than brevity" in user
    assert "the lower bound is a quality floor, not the true goal" in user
    assert "organize the script as a real lesson" in user
    assert "at least one concrete example, analogy, or comparison" in user


def test_script_generator_uses_more_internal_attempts():
    assert DefaultScriptGenerator.max_generation_attempts == 4


def test_narration_markup_issues_flags_markdown_not_plain_punctuation():
    text = "# Heading\n\n- Bullet point\n\n[Added context] Extra background."

    assert narration_markup_issues(text) == [
        "Markdown heading marker",
        "Markdown list marker",
        "inline added-context tag",
    ]
    assert narration_markup_issues("This sentence uses a dash-like pause, but it is not a list marker.") == []


def test_script_generator_retries_markdown_script_output():
    class FakeResponses:
        def __init__(self):
            self.inputs: list[str] = []
            self.calls = 0

        def parse(self, model, input, text_format):
            self.calls += 1
            self.inputs.append(input[-1]["content"])
            if self.calls == 1:
                script = "# Intro\n\n- This should not be spoken as Markdown.\n\n" + ("Plain sentence. " * 260)
            else:
                script = "Plain narration starts here. " + ("Plain sentence. " * 260)
            return SimpleNamespace(
                output_parsed=SimpleNamespace(
                    script_text=script,
                    added_context_notes=[],
                    validation_summary="ok",
                    corrected_or_clarified_points=[],
                    filled_gap_topics=[],
                )
            )

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

    generator = DefaultScriptGenerator(DummySettings())
    fake_client = FakeClient()
    generator.client = fake_client

    generated = generator.generate("Atoms", "source " * 100)

    assert generated.script_text.startswith("Plain narration starts here.")
    assert fake_client.responses.calls == 2
    assert "Previous attempt failed narration formatting validation" in fake_client.responses.inputs[1]
    assert "Markdown heading marker" in fake_client.responses.inputs[1]


def test_script_generation_requires_openai_key():
    generator = DefaultScriptGenerator(DummySettings())

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        generator.generate("Atoms", "source body")


def test_script_length_helpers_scale_for_large_lessons():
    normalized_words = 7109

    assert minimum_script_words(normalized_words) == 1200
    assert target_duration_minutes(normalized_words) == (8, 25)
    assert estimate_narration_minutes(1200) > 8
    assert script_meets_length_target(normalized_words, 1570) is True
    assert script_meets_length_target(normalized_words, 500) is False


def test_script_length_helpers_allow_study_worthy_mid_sized_lessons():
    normalized_words = 4997

    assert minimum_script_words(normalized_words) == 1499
    assert target_duration_minutes(normalized_words) == (8, 18)
    assert script_meets_length_target(normalized_words, 1493) is False
    assert script_meets_length_target(normalized_words, 1580) is True


def test_script_length_helpers_allow_cleaned_epistemology_sized_lessons():
    normalized_words = 3853

    assert minimum_script_words(normalized_words) == 1156
    assert target_duration_minutes(normalized_words) == (8, 18)
    assert script_meets_length_target(normalized_words, 1190) is True


def test_script_word_count_handles_plain_text():
    text = "Philosophy is the love of wisdom."

    assert script_word_count(text) == 6


def test_cleaned_source_material_removes_html_noise():
    text = "<h2>Epistemology</h2><p>What is knowledge?</p><p>https://example.com</p>"

    cleaned = cleaned_source_material(text)

    assert cleaned == "Epistemology\n\nWhat is knowledge?"


def test_script_gate_uses_cleaned_source_word_count():
    raw = "<h2>Epistemology</h2><p>Study of knowledge.</p>" * 200

    cleaned_words = script_word_count(cleaned_source_material(raw))
    raw_words = script_word_count(raw)

    assert cleaned_words < raw_words
    assert minimum_script_words(cleaned_words) < minimum_script_words(raw_words)


def test_flashcard_prompt_policy_forbids_vague_and_yes_no_cards():
    generator = DefaultFlashcardGenerator(DummySettings())

    messages = generator.build_prompt("Atoms", "The nucleus contains protons and neutrons.")
    system = messages[0]["content"]

    assert "yes/no" in system
    assert "key idea" in system
    assert "atomic fact" in system


def test_flashcard_target_card_count_scales_with_script_length():
    generator = DefaultFlashcardGenerator(DummySettings())

    short_count = generator.target_card_count("word " * 60)
    long_count = generator.target_card_count("word " * 3600)

    assert short_count == 6
    assert long_count == 30


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_sync_flashcards_keeps_matched_cards_and_replaces_others():
    session = _make_session()
    course = Course(trilium_note_id="course-1", title="Course", traversal_hash="h")
    session.add(course)
    session.flush()
    lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson", content_hash="h")
    session.add(lesson)
    session.flush()

    kept = Flashcard(lesson_id=lesson.id, prompt="What is X?", answer="old answer", due_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    kept.stability = 5.0
    kept.repetitions = 3
    stale = Flashcard(lesson_id=lesson.id, prompt="Unrelated stale card?", answer="stale", due_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    session.add_all([kept, stale])
    session.commit()
    kept_id = kept.id

    cards = [
        GeneratedFlashcard(prompt="What is X?", answer="new answer", source_excerpt="excerpt"),
        GeneratedFlashcard(prompt="What is Y?", answer="y answer", source_excerpt=None),
    ]
    sync_flashcards(session, lesson, cards)
    session.commit()

    remaining = {card.prompt: card for card in session.query(Flashcard).all()}
    assert set(remaining) == {"What is X?", "What is Y?"}
    matched = remaining["What is X?"]
    assert matched.id == kept_id
    assert matched.answer == "new answer"
    assert matched.stability == 5.0
    assert matched.repetitions == 3
    new_card = remaining["What is Y?"]
    assert new_card.stability is None


def test_sync_flashcards_unsuspends_changed_cards_and_dedupes_generated_prompts():
    session = _make_session()
    course = Course(trilium_note_id="course-2", title="Course", traversal_hash="h")
    session.add(course)
    session.flush()
    lesson = Lesson(course_id=course.id, trilium_note_id="lesson-2", title="Lesson", content_hash="h")
    session.add(lesson)
    session.flush()

    changed = Flashcard(
        lesson_id=lesson.id, prompt="Changed card?", answer="old", suspended=True,
        due_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    unchanged = Flashcard(
        lesson_id=lesson.id, prompt="Unchanged card?", answer="same", suspended=True,
        due_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add_all([changed, unchanged])
    session.commit()

    cards = [
        GeneratedFlashcard(prompt="Changed card?", answer="rewritten", source_excerpt=None),
        GeneratedFlashcard(prompt="Unchanged card?", answer="same", source_excerpt=None),
        GeneratedFlashcard(prompt="Duplicate prompt?", answer="first variant", source_excerpt=None),
        GeneratedFlashcard(prompt="Duplicate prompt?", answer="second variant", source_excerpt=None),
    ]
    sync_flashcards(session, lesson, cards)
    session.commit()

    by_prompt = {card.prompt: card for card in session.query(Flashcard).all()}
    # Regenerated content wakes a suspended card; untouched content stays parked.
    assert by_prompt["Changed card?"].suspended is False
    assert by_prompt["Changed card?"].answer == "rewritten"
    assert by_prompt["Unchanged card?"].suspended is True
    # Duplicate generated prompts collapse to the first variant, inserted once.
    duplicates = [card for card in by_prompt.values() if card.prompt == "Duplicate prompt?"]
    assert len(duplicates) == 1
    assert duplicates[0].answer == "first variant"


def test_default_kokoro_command_targets_repo_module():
    command = default_kokoro_command()

    assert "-m app.kokoro_cli" in command
    assert "{input}" in command
    assert "{output}" in command


def test_configure_tts_runtime_warnings_is_safe_without_torch(monkeypatch):
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    configure_tts_runtime_warnings()


def test_configure_tts_runtime_warnings_exports_hf_token(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ImportError
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    configure_tts_runtime_warnings("secret-token")

    assert os.environ["HF_TOKEN"] == "secret-token"
    assert os.environ["HUGGING_FACE_HUB_TOKEN"] == "secret-token"


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


class DummyStatusLesson:
    def __init__(self, normalized_content_path: str | None = None, script_provenance: str | None = None, artifacts=None):
        self.normalized_content_path = normalized_content_path
        self.script_provenance = script_provenance
        self.artifacts = artifacts or []


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


def test_media_duration_seconds_uses_ffprobe(monkeypatch, tmp_path: Path):
    def fake_run(command, check, capture_output=False, text=False):
        assert command[0] == "ffprobe"
        assert capture_output is True
        assert text is True
        return SimpleNamespace(stdout="91.2\n", stderr="")

    monkeypatch.setattr("app.pipeline.subprocess.run", fake_run)

    assert media_duration_seconds(tmp_path / "sample.wav") == 91.2


def test_command_tts_generator_records_duration(monkeypatch, tmp_path: Path):
    calls: list[object] = []

    def fake_run(command, shell=False, check=False, capture_output=False, text=False):
        calls.append(command)
        if capture_output:
            return SimpleNamespace(stdout="48.75\n", stderr="")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr("app.pipeline.subprocess.run", fake_run)

    generator = CommandTTSGenerator(DummySettings())
    output = generator.generate(DummyLesson(), "Script body", tmp_path / "audio.wav")

    assert isinstance(calls[0], str)
    assert "kokoro-cli --input " in calls[0]
    assert " --output " in calls[0]
    assert output["duration_seconds"] == 48.75


def test_large_lessons_get_larger_stage_estimates(tmp_path: Path):
    small_path = tmp_path / "small.md"
    large_path = tmp_path / "large.md"
    small_path.write_text("word " * 400, encoding="utf-8")
    large_path.write_text("word " * 8000, encoding="utf-8")

    small = DummyStatusLesson(normalized_content_path=str(small_path))
    large = DummyStatusLesson(normalized_content_path=str(large_path))

    small_estimates = lesson_stage_estimates_seconds(small)
    large_estimates = lesson_stage_estimates_seconds(large)

    assert large_estimates["script"] > small_estimates["script"]
    assert large_estimates["audio"] > small_estimates["audio"]


def test_completed_lessons_show_actual_runtime_from_artifacts():
    lesson = DummyStatusLesson(
        script_provenance='{"estimated_narration_minutes": 4.0}',
        artifacts=[SimpleNamespace(artifact_type="video", metadata_json='{"duration_seconds": 612.4}')],
    )
    lesson.id = 1
    lesson.title = "What is Philosophy?"
    lesson.stage = "upload"
    lesson.stage_state = "completed"
    lesson.stage_error = None
    lesson.current_job_id = None
    lesson.audio_path = "audio.wav"
    lesson.video_path = "video.mp4"
    lesson.youtube_upload = None
    lesson.normalized_content_path = None

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        payload = lesson_status_payload(session, lesson, {})

    assert payload["eta_label"] == "Runtime"
    assert payload["eta_text"] == "10m 12s"
    assert payload["actual_duration_seconds"] == 612.4
