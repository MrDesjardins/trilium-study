from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.db import Base, make_session_factory
from app.jobs import MAX_AUTOMATIC_RETRIES, JobRunner, PipelineServices
from app.models import Course, Job, Lesson
from app.pipeline import GeneratedFlashcard, GeneratedScript, GeneratedUpload


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


class FlakyRenderer(FakeRenderer):
    def __init__(self):
        self.calls = 0

    def render(self, lesson, audio_path: Path, output_path: Path):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary ffmpeg failure")
        return super().render(lesson, audio_path, output_path)


class CountingCollector(FakeCollector):
    def __init__(self):
        self.calls = 0

    async def collect_lesson(self, lesson_note_id: str):
        self.calls += 1
        return await super().collect_lesson(lesson_note_id)


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


def test_job_runner_completes_lesson_pipeline(tmp_path: Path):
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
        course_id = course.id

    def services_factory():
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id)
    runner._run_job(job_id)

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert lesson.stage_state == "completed"
        assert lesson.video_path is not None
        assert lesson.youtube_upload is not None
        assert lesson.youtube_upload.video_url == "https://youtube.com/watch?v=abc123"
        assert len(lesson.flashcards) == 1
        assert job.state == "completed"


def test_job_runner_marks_failed_stage(tmp_path: Path):
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
        course_id = course.id

    class ExplodingRenderer(FakeRenderer):
        def render(self, lesson, audio_path: Path, output_path: Path):
            raise RuntimeError("ffmpeg failed")

    def services_factory():
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=ExplodingRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id)
    with session_factory() as session:
        job = session.get(Job, job_id)
        job.retry_count = MAX_AUTOMATIC_RETRIES
        session.commit()
    try:
        runner._run_job(job_id)
    except RuntimeError:
        pass

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert lesson.stage_state == "failed"
        assert job.state == "failed"
        assert "ffmpeg failed" in job.error


def test_job_runner_resumes_from_failed_stage_without_recollecting(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(
            course_id=course.id,
            trilium_note_id="lesson-1",
            title="Lesson 1",
            parent_note_id="course",
            content_hash="hash123",
            stage="upload",
            stage_state="failed",
            script_text="saved script",
            flashcard_source_text="saved script",
            normalized_content_path=str(tmp_path / "normalized.md"),
            audio_path=str(tmp_path / "audio.wav"),
            video_path=str(tmp_path / "video.mp4"),
        )
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id
        course_id = course.id

    (tmp_path / "normalized.md").write_text("Normalized", encoding="utf-8")
    (tmp_path / "audio.wav").write_text("audio", encoding="utf-8")
    (tmp_path / "video.mp4").write_text("video", encoding="utf-8")

    collector = CountingCollector()

    def services_factory():
        return PipelineServices(
            collector=collector,
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id)
    runner._run_job(job_id)

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert collector.calls == 0
        assert lesson.stage_state == "completed"
        assert job.state == "completed"


def test_create_lesson_job_resets_completed_lessons_to_collect_stage(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(
            course_id=course.id,
            trilium_note_id="lesson-1",
            title="Lesson 1",
            parent_note_id="course",
            content_hash="hash123",
            stage="upload",
            stage_state="completed",
        )
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id
        course_id = course.id

    def services_factory():
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id, force_regenerate=True)

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert job is not None
        assert lesson.stage == "collect"
        assert lesson.stage_state == "queued"


def test_job_runner_retries_failed_lesson_pipeline(tmp_path: Path):
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
        course_id = course.id

    flaky_renderer = FlakyRenderer()

    def services_factory():
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=FakeScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=flaky_renderer,
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id)
    runner._run_job(job_id)
    runner._run_job(job_id)

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert flaky_renderer.calls == 2
        assert lesson.stage_state == "completed"
        assert job.state == "completed"
        assert job.retry_count == 1
        retry_events = [event.event_type for event in job.events]
        assert "job_retry_scheduled" in retry_events


def test_job_runner_does_not_retry_configuration_failure(tmp_path: Path):
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
        course_id = course.id

    class MissingConfigScriptGenerator(FakeScriptGenerator):
        def generate(self, lesson_title: str, normalized_text: str):
            raise RuntimeError("OPENAI_API_KEY is required for validated script generation.")

    def services_factory():
        return PipelineServices(
            collector=FakeCollector(),
            script_generator=MissingConfigScriptGenerator(),
            flashcard_generator=FakeFlashcardGenerator(),
            tts_generator=FakeTTS(),
            video_renderer=FakeRenderer(),
            youtube_publisher=FakeYoutube(),
        )

    runner = JobRunner(settings, session_factory, services_factory)
    job_id = runner.create_lesson_job(course_id, lesson_id)
    try:
        runner._run_job(job_id)
    except RuntimeError:
        pass

    with session_factory() as session:
        lesson = session.get(Lesson, lesson_id)
        job = session.get(Job, job_id)
        assert lesson.stage_state == "failed"
        assert job.state == "failed"
        assert job.retry_count == 0
        retry_events = [event.event_type for event in job.events]
        assert "job_retry_scheduled" not in retry_events
