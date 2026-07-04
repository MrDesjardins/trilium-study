from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Base, make_session_factory
from app.jobs import PipelineServices
from app.models import Course, Lesson
from app.pipeline import GeneratedFlashcard, GeneratedScript, GeneratedUpload
from app.run_lesson import (
    lesson_workspace_relpaths,
    list_courses,
    list_lessons,
    pending_lesson_ids_for_course,
    run_lessons,
    sync_catalog_cli,
)


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


def test_list_lessons_hides_archived_by_default(tmp_path: Path, capsys):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id="catalog", traversal_hash="course")
        session.add(course)
        session.flush()
        session.add(
            Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="")
        )
        session.add(
            Lesson(
                course_id=course.id,
                trilium_note_id="lesson-2",
                title="Lesson 2",
                parent_note_id="course",
                content_hash="",
                archived_at=course.created_at,
            )
        )
        session.commit()

    list_lessons(settings)
    active_output = capsys.readouterr().out
    assert "Course / Lesson 1" in active_output
    assert "Lesson 2" not in active_output

    list_lessons(settings, include_archived=True)
    archived_output = capsys.readouterr().out
    assert "Course / Lesson 1" in archived_output
    assert "Course / Lesson 2" in archived_output
    assert "yes" in archived_output


def test_pending_lesson_ids_for_course_skips_completed_and_archived(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id="catalog", traversal_hash="course")
        other_course = Course(trilium_note_id="other", title="Other", parent_note_id="catalog", traversal_hash="other")
        archived_course = Course(
            trilium_note_id="archived",
            title="Archived",
            parent_note_id="catalog",
            traversal_hash="archived",
            archived_at=now,
        )
        session.add_all([course, other_course, archived_course])
        session.flush()
        pending = Lesson(course_id=course.id, trilium_note_id="l-pending", title="Pending", parent_note_id="course", content_hash="")
        failed = Lesson(
            course_id=course.id,
            trilium_note_id="l-failed",
            title="Failed",
            parent_note_id="course",
            content_hash="",
            stage="audio",
            stage_state="failed",
        )
        done = Lesson(
            course_id=course.id,
            trilium_note_id="l-done",
            title="Done",
            parent_note_id="course",
            content_hash="",
            stage="upload",
            stage_state="completed",
        )
        archived = Lesson(
            course_id=course.id,
            trilium_note_id="l-archived",
            title="Archived",
            parent_note_id="course",
            content_hash="",
            archived_at=now,
        )
        other = Lesson(course_id=other_course.id, trilium_note_id="l-other", title="Other", parent_note_id="other", content_hash="")
        session.add_all([pending, failed, done, archived, other])
        session.commit()
        course_id = course.id
        archived_course_id = archived_course.id
        expected_ids = [pending.id, failed.id]

    assert pending_lesson_ids_for_course(settings, course_id) == expected_ids

    with pytest.raises(SystemExit, match="not found"):
        pending_lesson_ids_for_course(settings, 9999)

    with pytest.raises(SystemExit, match="archived"):
        pending_lesson_ids_for_course(settings, archived_course_id)


def test_list_courses_shows_pending_counts(tmp_path: Path, capsys):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course A", parent_note_id="catalog", traversal_hash="course")
        session.add(course)
        session.flush()
        session.add(
            Lesson(course_id=course.id, trilium_note_id="l-1", title="L1", parent_note_id="course", content_hash="")
        )
        session.add(
            Lesson(
                course_id=course.id,
                trilium_note_id="l-2",
                title="L2",
                parent_note_id="course",
                content_hash="",
                stage="upload",
                stage_state="completed",
            )
        )
        session.commit()
        course_id = course.id

    list_courses(settings)
    output = capsys.readouterr().out
    assert "Course A" in output
    line = next(line for line in output.splitlines() if "Course A" in line)
    assert line.split()[0] == str(course_id)
    assert line.split()[1] == "1"


def test_sync_catalog_cli_reports_newly_archived_courses(tmp_path: Path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        # Active course that is NOT under the synced catalog note -> gets archived.
        stale = Course(trilium_note_id="stale-course", title="Stale Course", parent_note_id="old-catalog", traversal_hash="stale-course")
        session.add(stale)
        session.commit()

    class FakeTriliumClient:
        def __init__(self, *_args):
            self.notes = {
                "course": {"noteId": "course", "title": "University", "childNoteIds": ["class-1"]},
                "class-1": {"noteId": "class-1", "title": "Class 1", "childNoteIds": ["course-a"]},
                "course-a": {"noteId": "course-a", "title": "Course A", "childNoteIds": []},
            }

        async def get_note(self, note_id: str):
            return self.notes[note_id]

        async def get_course_lessons(self, parent_note_id: str):
            note = self.notes[parent_note_id]
            return [self.notes[child_id] for child_id in note["childNoteIds"]]

    monkeypatch.setattr("app.catalog.TriliumClient", FakeTriliumClient)
    sync_catalog_cli(settings)

    captured = capsys.readouterr()
    assert "SYNC_ARCHIVED_COURSES 1" in captured.out
    assert "Stale Course" in captured.err


def test_run_lessons_partial_failure_exits_2_with_results(tmp_path: Path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        good = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="")
        bad = Lesson(course_id=course.id, trilium_note_id="lesson-2", title="Lesson 2", parent_note_id="course", content_hash="")
        session.add_all([good, bad])
        session.commit()
        good_id = good.id
        bad_id = bad.id

    class FailingTTS:
        def generate(self, lesson, script_text: str, output_path: Path):
            if lesson.trilium_note_id == "lesson-2":
                raise RuntimeError("TTS exploded")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("audio", encoding="utf-8")
            return {"path": str(output_path)}

    def fake_services_factory(settings: Settings):
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FailingTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    monkeypatch.setattr("app.run_lesson.services_factory", fake_services_factory)
    with pytest.raises(SystemExit) as excinfo:
        run_lessons(settings, [good_id, bad_id])
    assert excinfo.value.code == 2

    output = capsys.readouterr().out
    assert f"LESSON_RESULT {good_id} completed" in output
    assert f"LESSON_RESULT {bad_id} failed" in output
    assert "Summary:" in output

    with session_factory() as session:
        assert session.get(Lesson, good_id).stage_state == "completed"
        assert session.get(Lesson, bad_id).stage_state == "failed"
