from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db import Base, make_session_factory
from app.jobs import PipelineServices
from app.models import Course, Lesson
from app.pipeline import GeneratedFlashcard, GeneratedScript, GeneratedUpload
from app.run_lesson import lesson_workspace_relpaths, run_lessons


class FakeCollector:
    async def collect_lesson(self, lesson_note_id: str):
        class Snapshot:
            title = "Lesson"
            content_hash = "hash123"
            note_tree = {"note_id": lesson_note_id}
            normalized_text = "Normalized"

        return Snapshot()


class FakeScriptGenerator:
    def generate(self, lesson_title: str, normalized_text: str):
        return GeneratedScript(script_text=f"{lesson_title}: {normalized_text}", provenance={"mode": "fake"})


class FakeFlashcardGenerator:
    def generate(self, lesson_title: str, script_text: str):
        return [GeneratedFlashcard(prompt="Q", answer="A", source_excerpt="E")]


class FakeTTS:
    def generate(self, lesson, script_text: str, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("audio", encoding="utf-8")
        return {"path": str(output_path)}


class FakeRenderer:
    def render(self, lesson, audio_path: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("video", encoding="utf-8")
        return {"path": str(output_path)}


class FakeYoutube:
    def upload(self, lesson, video_path: Path):
        return GeneratedUpload(video_id="abc123", video_url="https://youtube.com/watch?v=abc123", response={"ok": True})


def make_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "TRILIUM_URL": "http://example",
            "TRILIUM_ETAPI_TOKEN": "token",
            "TRILIUM_PARENT_NOTE_ID": "course",
            "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
            "WORKSPACE_DIR": str(tmp_path / "workspace"),
            "LOG_DIR": str(tmp_path / "logs"),
        }
    )


def test_run_lesson_completes_pipeline(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="")
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id

    def fake_services_factory(settings: Settings):
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    monkeypatch.setattr("app.run_lesson.services_factory", fake_services_factory)
    run_lessons(settings, [lesson_id])

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        assert lesson is not None
        assert lesson.stage_state == "completed"
        assert lesson.video_path is not None


def test_lesson_workspace_relpaths(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="")
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id

    paths = lesson_workspace_relpaths(settings, [lesson_id])
    assert len(paths) == 1
    assert paths[0] == "course/course-lesson-1-lesson-1"
