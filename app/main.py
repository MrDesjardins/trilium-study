from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bootstrap import bootstrap_database
from app.config import Settings, get_settings
from app.content import LessonCollector, TriliumClient, TriliumClientError
from app.db import make_session_factory
from app.jobs import JobRunner, PipelineServices
from app.logging_utils import configure_logging
from app.models import Course, Flashcard, FlashcardReview, JobEvent, Lesson
from app.pipeline import (
    CommandTTSGenerator,
    DefaultFlashcardGenerator,
    DefaultScriptGenerator,
    FfmpegVideoRenderer,
    YouTubeApiPublisher,
    create_flashcard_review,
    schedule_flashcard_review,
)
from app.status import lesson_status_payload, queue_positions, runtime_checks


async def enqueue_audio_stream(queue_url: str, youtube_url: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(queue_url, json={"youtube_video_id": youtube_url})
        response.raise_for_status()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    session_factory, _ = make_session_factory(settings)
    bootstrap_database(settings)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    def services_factory() -> PipelineServices:
        trilium_client = TriliumClient(settings.trilium_url, settings.trilium_etapi_token)
        return PipelineServices(
            collector=LessonCollector(trilium_client),
            script_generator=DefaultScriptGenerator(settings),
            flashcard_generator=DefaultFlashcardGenerator(settings),
            tts_generator=CommandTTSGenerator(settings),
            video_renderer=FfmpegVideoRenderer(settings),
            youtube_publisher=YouTubeApiPublisher(settings),
        )

    runner = JobRunner(settings, session_factory, services_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runner.start()
        yield
        runner.stop()

    app = FastAPI(title="Trilium Study", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    def get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_or_create_default_course(session: Session) -> Course:
        course = session.scalar(select(Course).order_by(Course.created_at.asc()))
        if course is None:
            course = Course(
                trilium_note_id=settings.trilium_parent_note_id,
                title="Configured Trilium Course",
                parent_note_id=None,
                traversal_hash=settings.trilium_parent_note_id,
            )
            session.add(course)
            session.flush()
        return course

    def pop_flash(request: Request) -> tuple[str | None, str | None]:
        flash_level = request.cookies.get("flash_level")
        flash_message = request.cookies.get("flash_message")
        return flash_level, flash_message

    def redirect_with_flash(url: str, message: str, level: str = "info") -> RedirectResponse:
        response = RedirectResponse(url=url, status_code=303)
        response.set_cookie("flash_level", level, max_age=60, httponly=True, samesite="lax")
        response.set_cookie("flash_message", message, max_age=60, httponly=True, samesite="lax")
        return response

    def study_stats_payload(session: Session, now: datetime) -> dict[str, int]:
        active_cards = Flashcard.suspended.is_(False)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        total_cards = session.scalar(select(func.count()).select_from(Flashcard).where(active_cards)) or 0
        due_count = session.scalar(select(func.count()).select_from(Flashcard).where(active_cards, Flashcard.due_at <= now)) or 0
        reviewed_today = (
            session.scalar(
                select(func.count())
                .select_from(FlashcardReview)
                .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
                .where(active_cards, FlashcardReview.reviewed_at >= day_start)
            )
            or 0
        )
        passed_today = (
            session.scalar(
                select(func.count())
                .select_from(FlashcardReview)
                .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
                .where(active_cards, FlashcardReview.reviewed_at >= day_start, FlashcardReview.result == "pass")
            )
            or 0
        )
        failed_today = (
            session.scalar(
                select(func.count())
                .select_from(FlashcardReview)
                .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
                .where(active_cards, FlashcardReview.reviewed_at >= day_start, FlashcardReview.result == "again")
            )
            or 0
        )
        return {
            "total_cards": total_cards,
            "due_now": due_count,
            "remaining_after_current": max(due_count - 1, 0),
            "reviewed_today": reviewed_today,
            "passed_today": passed_today,
            "failed_today": failed_today,
        }

    def ordered_flashcards(session: Session) -> list[Flashcard]:
        return session.scalars(
            select(Flashcard).where(Flashcard.suspended.is_(False)).order_by(Flashcard.due_at.asc(), Flashcard.id.asc())
        ).all()

    def browse_payload(cards: list[Flashcard], current_id: int | None = None) -> dict | None:
        if not cards:
            return None
        index = 0
        if current_id is not None:
            for idx, card in enumerate(cards):
                if card.id == current_id:
                    index = idx
                    break
        card = cards[index]
        return {
            "flashcard": card,
            "position": index + 1,
            "total": len(cards),
            "previous_id": cards[index - 1].id if index > 0 else None,
            "next_id": cards[index + 1].id if index + 1 < len(cards) else None,
        }

    def study_response(
        request: Request,
        flashcard: Flashcard | None,
        stats: dict[str, int],
        browse_state: dict | None,
        review_mode: bool,
        flash_level: str | None,
        flash_message: str | None,
    ) -> HTMLResponse:
        trilium_note_url = None
        if flashcard and flashcard.lesson:
            trilium_note_url = f"{settings.trilium_url.rstrip('/')}/#root/{flashcard.lesson.trilium_note_id}"
        response = templates.TemplateResponse(
            request,
            "study.html",
            {
                "request": request,
                "flashcard": flashcard,
                "trilium_note_url": trilium_note_url,
                "stats": stats,
                "review_mode": review_mode,
                "browse_state": browse_state,
                "browse_entry_url": "/study/browse" if stats["total_cards"] else None,
                "flash_level": flash_level,
                "flash_message": flash_message,
            },
        )
        if flash_message:
            response.delete_cookie("flash_level")
            response.delete_cookie("flash_message")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, db: Session = Depends(get_db)):
        course = get_or_create_default_course(db)
        lessons = db.scalars(select(Lesson).where(Lesson.course_id == course.id).order_by(Lesson.created_at)).all()
        jobs = db.scalars(select(JobEvent).order_by(JobEvent.created_at.desc()).limit(25)).all()
        queue_map = queue_positions(db)
        lesson_statuses = {lesson.id: lesson_status_payload(db, lesson, queue_map) for lesson in lessons}
        flash_level, flash_message = pop_flash(request)
        checks = runtime_checks(settings)
        failing_runtime_checks = [check for check in checks if not check.ok]
        response = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "course": course,
                "lessons": lessons,
                "lesson_statuses": lesson_statuses,
                "events": jobs,
                "now": datetime.now(timezone.utc),
                "flash_level": flash_level,
                "flash_message": flash_message,
                "runtime_checks": checks,
                "failing_runtime_checks": failing_runtime_checks,
            },
        )
        if flash_message:
            response.delete_cookie("flash_level")
            response.delete_cookie("flash_message")
        return response

    @app.post("/courses/sync")
    async def sync_course(parent_note_id: str = Form(default=""), db: Session = Depends(get_db)):
        course = get_or_create_default_course(db)
        parent_note_id = (parent_note_id or course.trilium_note_id).strip()
        client = TriliumClient(settings.trilium_url, settings.trilium_etapi_token)
        try:
            parent_note = await client.get_note(parent_note_id)
            lesson_notes = await client.get_course_lessons(parent_note_id)
        except TriliumClientError as exc:
            return redirect_with_flash("/", str(exc), level="error")
        course.trilium_note_id = parent_note_id
        course.title = parent_note.get("title", course.title)
        course.traversal_hash = parent_note_id
        note_ids = set()
        for note in lesson_notes:
            note_id = note["noteId"]
            note_ids.add(note_id)
            existing = db.scalar(select(Lesson).where(Lesson.course_id == course.id, Lesson.trilium_note_id == note_id))
            if existing is None:
                db.add(
                    Lesson(
                        course_id=course.id,
                        trilium_note_id=note_id,
                        title=note.get("title", note_id),
                        parent_note_id=course.trilium_note_id,
                        content_hash="",
                        stage="pending",
                        stage_state="pending",
                    )
                )
            else:
                existing.title = note.get("title", existing.title)
        for lesson in db.scalars(select(Lesson).where(Lesson.course_id == course.id)).all():
            if lesson.trilium_note_id not in note_ids:
                db.delete(lesson)
        db.commit()
        if not lesson_notes:
            return redirect_with_flash("/", f"No direct child lessons found under note {parent_note_id}.")
        return redirect_with_flash("/", f"Discovered {len(lesson_notes)} direct child lessons under {parent_note_id}.")

    @app.post("/courses/generate")
    async def generate_selected_lessons(
        lesson_ids: list[int] = Form(default=[]),
        force_regenerate: bool = Form(default=False),
        db: Session = Depends(get_db),
    ):
        selected = [db.get(Lesson, lesson_id) for lesson_id in lesson_ids]
        selected_lessons = [lesson for lesson in selected if lesson is not None]
        if not selected_lessons:
            return redirect_with_flash("/", "No lessons selected for generation.")
        for lesson in selected_lessons:
            runner.create_lesson_job(lesson.course_id, lesson.id, force_regenerate=force_regenerate)
        return redirect_with_flash("/", f"Queued {len(selected_lessons)} lesson generation job(s).")

    @app.post("/lessons/{lesson_id}/run")
    async def run_lesson(lesson_id: int, force_regenerate: bool = Form(default=False), db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        runner.create_lesson_job(lesson.course_id, lesson.id, force_regenerate=force_regenerate)
        return redirect_with_flash("/", f"Queued lesson '{lesson.title}' for generation.")

    @app.get("/lessons/{lesson_id}", response_class=HTMLResponse)
    async def lesson_detail(request: Request, lesson_id: int, db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        events = []
        if lesson.current_job_id is not None:
            events = db.scalars(select(JobEvent).where(JobEvent.job_id == lesson.current_job_id).order_by(JobEvent.created_at.desc())).all()
        status = lesson_status_payload(db, lesson, queue_positions(db))
        flash_level, flash_message = pop_flash(request)
        response = templates.TemplateResponse(
            request,
            "lesson_detail.html",
            {"request": request, "lesson": lesson, "events": events, "status": status, "flash_level": flash_level, "flash_message": flash_message},
        )
        if flash_message:
            response.delete_cookie("flash_level")
            response.delete_cookie("flash_message")
        return response

    @app.get("/api/dashboard")
    async def dashboard_api(db: Session = Depends(get_db)):
        course = get_or_create_default_course(db)
        lessons = db.scalars(select(Lesson).where(Lesson.course_id == course.id).order_by(Lesson.created_at)).all()
        queue_map = queue_positions(db)
        return {
            "lessons": [lesson_status_payload(db, lesson, queue_map) for lesson in lessons],
            "runtime_checks": [check.__dict__ for check in runtime_checks(settings)],
        }

    @app.get("/study", response_class=HTMLResponse)
    async def study(request: Request, db: Session = Depends(get_db)):
        now = datetime.now(timezone.utc)
        flash_level, flash_message = pop_flash(request)
        stats = study_stats_payload(db, now)
        flashcard = db.scalar(
            select(Flashcard).where(Flashcard.suspended.is_(False), Flashcard.due_at <= now).order_by(Flashcard.due_at.asc())
        )
        return study_response(request, flashcard, stats, None, True, flash_level, flash_message)

    @app.get("/study/browse", response_class=HTMLResponse)
    @app.get("/study/browse/{flashcard_id}", response_class=HTMLResponse)
    async def study_browse(request: Request, flashcard_id: int | None = None, db: Session = Depends(get_db)):
        now = datetime.now(timezone.utc)
        flash_level, flash_message = pop_flash(request)
        stats = study_stats_payload(db, now)
        state = browse_payload(ordered_flashcards(db), flashcard_id)
        flashcard = state["flashcard"] if state else None
        return study_response(request, flashcard, stats, state, False, flash_level, flash_message)

    @app.post("/study/{flashcard_id}/review")
    async def review_flashcard(flashcard_id: int, result: str = Form(), db: Session = Depends(get_db)):
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        outcome = schedule_flashcard_review(flashcard, "pass" if result == "pass" else "again")
        review = create_flashcard_review(flashcard, outcome)
        db.add(review)
        db.commit()
        return RedirectResponse(url="/study", status_code=303)

    @app.post("/study/reset")
    async def reset_flashcards(db: Session = Depends(get_db)):
        flashcards = db.scalars(select(Flashcard).where(Flashcard.suspended.is_(False))).all()
        if not flashcards:
            return redirect_with_flash("/study", "No flashcards available to reset.")
        now = datetime.now(timezone.utc)
        for flashcard in flashcards:
            flashcard.ease_factor = 2.5
            flashcard.interval_days = 0
            flashcard.repetitions = 0
            flashcard.due_at = now
            flashcard.suspended = False
        db.commit()
        return redirect_with_flash("/study", f"Reset {len(flashcards)} flashcard(s) for a fresh study pass.")

    @app.get("/api/lessons/{lesson_id}")
    async def lesson_api(lesson_id: int, db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return lesson_status_payload(db, lesson, queue_positions(db))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/lessons/{lesson_id}/queue-audio")
    async def queue_audio_lesson(lesson_id: int, request: Request, db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        redirect_url = request.headers.get("referer") or "/"
        youtube_url = lesson.youtube_upload.video_url if lesson.youtube_upload else None
        if not youtube_url:
            return redirect_with_flash(redirect_url, f"{lesson.title} does not have a YouTube upload yet.", level="error")
        try:
            await enqueue_audio_stream(settings.audio_queue_url, youtube_url)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip() or f"HTTP {exc.response.status_code}"
            return redirect_with_flash(redirect_url, f"Audio queue rejected {lesson.title}: {detail}", level="error")
        except httpx.HTTPError as exc:
            return redirect_with_flash(redirect_url, f"Audio queue is unavailable for {lesson.title}: {exc}", level="error")
        return redirect_with_flash(redirect_url, f"Queued {lesson.title} for the audio YouTube stream.")

    return app


try:
    app = create_app()
except Exception as exc:  # pragma: no cover
    fallback = FastAPI(title="Trilium Study (configuration error)")

    @fallback.get("/healthz")
    async def healthz():
        return {"ok": False, "error": str(exc)}

    app = fallback
