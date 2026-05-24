from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.content import LessonCollector
from app.logging_utils import get_logger
from app.models import Course, Job, JobEvent, Lesson
from app.pipeline import (
    STAGES,
    CommandTTSGenerator,
    DefaultFlashcardGenerator,
    DefaultScriptGenerator,
    FfmpegVideoRenderer,
    YouTubeApiPublisher,
    ensure_artifact,
    replace_flashcards,
    should_skip_stage,
    upsert_youtube_upload,
)
from app.storage import artifact_path, hashed_filename

logger = get_logger(__name__)
MAX_AUTOMATIC_RETRIES = 2
NON_RETRYABLE_ERROR_MARKERS = (
    "openai_api_key is required",
    "youtube token file not found",
    "youtube credentials are invalid",
    "youtube data api v3 is disabled",
    "dependencies are not installed",
    "not installed. run `uv sync",
    "is not configured",
    "token file not found",
    "could not determine audio duration",
    "returned an invalid audio duration",
    "audio duration must be positive",
)


@dataclass
class PipelineServices:
    collector: LessonCollector
    script_generator: DefaultScriptGenerator
    flashcard_generator: DefaultFlashcardGenerator
    tts_generator: CommandTTSGenerator
    video_renderer: FfmpegVideoRenderer
    youtube_publisher: YouTubeApiPublisher


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        services_factory: Callable[[], PipelineServices],
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.services_factory = services_factory
        self._queue: queue.Queue[int] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._work_loop, name="job-runner", daemon=True)

    def start(self) -> None:
        self._resume_incomplete_jobs()
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(-1)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def enqueue(self, job_id: int) -> None:
        self._queue.put(job_id)

    def create_course_job(self, course_id: int, force_regenerate: bool = False) -> int:
        with self.session_factory() as session:
            job = Job(course_id=course_id, lesson_id=None, job_type="course_sync", state="pending", force_regenerate=force_regenerate)
            session.add(job)
            session.commit()
            self.enqueue(job.id)
            return job.id

    def create_lesson_job(self, course_id: int, lesson_id: int, force_regenerate: bool = False) -> int:
        with self.session_factory() as session:
            existing = session.scalar(
                select(Job).where(
                    Job.course_id == course_id,
                    Job.lesson_id == lesson_id,
                    Job.job_type == "lesson_pipeline",
                    Job.state.in_(["pending", "running"]),
                )
            )
            if existing is not None:
                return existing.id
            job = Job(course_id=course_id, lesson_id=lesson_id, job_type="lesson_pipeline", state="pending", force_regenerate=force_regenerate)
            session.add(job)
            session.flush()
            lesson = session.get(Lesson, lesson_id)
            if lesson is not None:
                lesson.current_job_id = job.id
                if lesson.stage_state != "failed":
                    lesson.stage_state = "queued"
                    lesson.stage = STAGES[0]
                elif lesson.stage not in STAGES:
                    lesson.stage = STAGES[0]
            session.commit()
            self.enqueue(job.id)
            return job.id

    def _resume_incomplete_jobs(self) -> None:
        with self.session_factory() as session:
            jobs = session.scalars(select(Job).where(Job.state.in_(["pending", "running"]))).all()
            for job in jobs:
                if job.state == "running":
                    job.state = "pending"
                    job.error = "Recovered after restart"
                self._queue.put(job.id)
            session.commit()

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id < 0:
                return
            try:
                self._run_job(job_id)
            except Exception as exc:  # pragma: no cover
                logger.exception("job_failed", extra={"extra_data": {"job_id": job_id, "error": str(exc)}})

    def _run_job(self, job_id: int) -> None:
        services = self.services_factory()
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.state = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            self._event(session, job, "job_started", f"Started job {job.job_type}")
            session.commit()
        try:
            if job.job_type == "course_sync":
                self._run_course_sync(job_id, services)
            else:
                self._run_lesson_pipeline(job_id, services)
        except Exception:
            if self._schedule_retry(job_id):
                return
            raise

    def _schedule_retry(self, job_id: int) -> bool:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None or job.job_type != "lesson_pipeline" or job.state != "failed":
                return False
            if not self._is_retryable_error(job.error):
                return False
            if job.retry_count >= MAX_AUTOMATIC_RETRIES:
                return False
            job.retry_count += 1
            job.state = "pending"
            job.finished_at = None
            self._event(
                session,
                job,
                "job_retry_scheduled",
                f"Retry {job.retry_count} of {MAX_AUTOMATIC_RETRIES} scheduled after failure",
                stage=job.current_stage,
            )
            session.commit()
        self.enqueue(job_id)
        return True

    @staticmethod
    def _is_retryable_error(error: str | None) -> bool:
        if not error:
            return True
        normalized = error.lower()
        return not any(marker in normalized for marker in NON_RETRYABLE_ERROR_MARKERS)

    def _run_course_sync(self, job_id: int, services: PipelineServices) -> None:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            course = session.get(Course, job.course_id) if job else None
            if job is None or course is None:
                return
            lessons = session.scalars(select(Lesson).where(Lesson.course_id == course.id)).all()
            for lesson in lessons:
                lesson_job_id = self.create_lesson_job(course.id, lesson.id, force_regenerate=job.force_regenerate)
                self._event(session, job, "lesson_enqueued", f"Enqueued lesson job {lesson_job_id} for {lesson.title}")
            job.state = "completed"
            job.finished_at = datetime.now(timezone.utc)
            self._event(session, job, "job_completed", "Course sync enqueued lessons")
            session.commit()

    def _run_lesson_pipeline(self, job_id: int, services: PipelineServices) -> None:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            lesson = session.get(Lesson, job.lesson_id) if job else None
            if job is None or lesson is None:
                return
            course = session.get(Course, lesson.course_id)
            lesson.current_job_id = job.id
            lesson.stage_error = None
            session.commit()

            try:
                snapshot = services.collector.collect_lesson
                for stage in self._stages_for_run(lesson, job.force_regenerate):
                    job.current_stage = stage
                    lesson.stage = stage
                    lesson.stage_state = "running"
                    self._event(session, job, "stage_started", f"Stage {stage} started", stage=stage)
                    session.commit()

                    if stage == "collect":
                        collected = run_async(snapshot(lesson.trilium_note_id))
                        lesson.content_hash = collected.content_hash
                        raw_path = artifact_path(
                            self.settings.workspace_path, course, lesson, "collect", hashed_filename("raw", collected.content_hash, "json")
                        )
                        raw_path.parent.mkdir(parents=True, exist_ok=True)
                        raw_path.write_text(json.dumps(collected.note_tree, ensure_ascii=True, indent=2), encoding="utf-8")
                        lesson.raw_snapshot_path = str(raw_path)
                        artifact = ensure_artifact(session, lesson, "raw_snapshot", stage, collected.content_hash)
                        artifact.state = "completed"
                        artifact.path = str(raw_path)

                    elif stage == "normalize":
                        if should_skip_stage(session, lesson, "normalized_content", stage, lesson.content_hash, job.force_regenerate):
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Normalized content reused", stage=stage)
                        else:
                            collected = run_async(snapshot(lesson.trilium_note_id))
                            normalized_path = artifact_path(
                                self.settings.workspace_path,
                                course,
                                lesson,
                                "normalize",
                                hashed_filename("normalized", lesson.content_hash, "md"),
                            )
                            normalized_path.parent.mkdir(parents=True, exist_ok=True)
                            normalized_path.write_text(collected.normalized_text, encoding="utf-8")
                            lesson.normalized_content_path = str(normalized_path)
                            artifact = ensure_artifact(session, lesson, "normalized_content", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.path = str(normalized_path)

                    elif stage == "script":
                        if should_skip_stage(session, lesson, "script", stage, lesson.content_hash, job.force_regenerate) and lesson.script_text:
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Script reused", stage=stage)
                        else:
                            normalized_text = Path(lesson.normalized_content_path).read_text(encoding="utf-8")
                            generated = services.script_generator.generate(lesson.title, normalized_text)
                            lesson.script_text = generated.script_text
                            lesson.script_provenance = json.dumps(generated.provenance, ensure_ascii=True)
                            lesson.flashcard_source_text = generated.script_text
                            script_path = artifact_path(
                                self.settings.workspace_path, course, lesson, "script", hashed_filename("script", lesson.content_hash, "md")
                            )
                            script_path.parent.mkdir(parents=True, exist_ok=True)
                            script_path.write_text(generated.script_text, encoding="utf-8")
                            artifact = ensure_artifact(session, lesson, "script", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.path = str(script_path)
                            artifact.metadata_json = lesson.script_provenance

                    elif stage == "flashcards":
                        if should_skip_stage(session, lesson, "flashcards", stage, lesson.content_hash, job.force_regenerate) and lesson.flashcards:
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Flashcards reused", stage=stage)
                        else:
                            cards = services.flashcard_generator.generate(lesson.title, lesson.flashcard_source_text or lesson.script_text or "")
                            replace_flashcards(session, lesson, cards)
                            flash_path = artifact_path(
                                self.settings.workspace_path, course, lesson, "flashcards", hashed_filename("flashcards", lesson.content_hash, "json")
                            )
                            flash_path.parent.mkdir(parents=True, exist_ok=True)
                            flash_path.write_text(json.dumps([card.__dict__ for card in cards], ensure_ascii=True, indent=2), encoding="utf-8")
                            artifact = ensure_artifact(session, lesson, "flashcards", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.path = str(flash_path)

                    elif stage == "audio":
                        if should_skip_stage(session, lesson, "audio", stage, lesson.content_hash, job.force_regenerate) and lesson.audio_path:
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Audio reused", stage=stage)
                        else:
                            audio_path = artifact_path(
                                self.settings.workspace_path, course, lesson, "audio", hashed_filename("audio", lesson.content_hash, "wav")
                            )
                            metadata = services.tts_generator.generate(lesson, lesson.script_text or "", audio_path)
                            lesson.audio_path = str(audio_path)
                            artifact = ensure_artifact(session, lesson, "audio", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.path = str(audio_path)
                            artifact.metadata_json = json.dumps(metadata, ensure_ascii=True)

                    elif stage == "video":
                        if should_skip_stage(session, lesson, "video", stage, lesson.content_hash, job.force_regenerate) and lesson.video_path:
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Video reused", stage=stage)
                        else:
                            video_path = artifact_path(
                                self.settings.workspace_path, course, lesson, "video", hashed_filename("video", lesson.content_hash, "mp4")
                            )
                            metadata = services.video_renderer.render(lesson, Path(lesson.audio_path), video_path)
                            lesson.video_path = str(video_path)
                            artifact = ensure_artifact(session, lesson, "video", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.path = str(video_path)
                            artifact.metadata_json = json.dumps(metadata, ensure_ascii=True)

                    elif stage == "upload":
                        if should_skip_stage(session, lesson, "youtube_upload", stage, lesson.content_hash, job.force_regenerate) and lesson.youtube_upload:
                            lesson.stage_state = "skipped"
                            self._event(session, job, "stage_skipped", "Upload reused", stage=stage)
                        else:
                            upload = services.youtube_publisher.upload(lesson, Path(lesson.video_path))
                            upsert_youtube_upload(session, lesson, upload)
                            artifact = ensure_artifact(session, lesson, "youtube_upload", stage, lesson.content_hash)
                            artifact.state = "completed"
                            artifact.metadata_json = json.dumps(upload.response, ensure_ascii=True)

                    if lesson.stage_state != "skipped":
                        lesson.stage_state = "completed"
                    lesson.last_processed_at = datetime.now(timezone.utc)
                    self._event(session, job, "stage_completed", f"Stage {stage} completed", stage=stage)
                    session.commit()

                lesson.stage = STAGES[-1]
                lesson.stage_state = "completed"
                lesson.stage_error = None
                job.state = "completed"
                job.finished_at = datetime.now(timezone.utc)
                self._event(session, job, "job_completed", f"Lesson pipeline completed for {lesson.title}")
                session.commit()

            except Exception as exc:
                job.state = "failed"
                job.error = str(exc)
                job.finished_at = datetime.now(timezone.utc)
                lesson.stage_state = "failed"
                lesson.stage_error = str(exc)
                lesson.last_processed_at = datetime.now(timezone.utc)
                self._event(session, job, "job_failed", str(exc), stage=job.current_stage)
                session.commit()
                raise

    def _event(self, session: Session, job: Job, event_type: str, message: str, stage: str | None = None) -> None:
        session.add(JobEvent(job_id=job.id, event_type=event_type, message=message, stage=stage))

    @staticmethod
    def _stages_for_run(lesson: Lesson, force_regenerate: bool) -> list[str]:
        if force_regenerate:
            return STAGES
        if lesson.stage_state == "failed" and lesson.stage in STAGES:
            return STAGES[STAGES.index(lesson.stage) :]
        return STAGES


def run_async(awaitable):
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, awaitable).result()
    return asyncio.run(awaitable)
