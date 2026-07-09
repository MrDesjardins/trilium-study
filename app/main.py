from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.bootstrap import bootstrap_database
from app.catalog import sync_catalog
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
)
from app.srs import (
    apply_review,
    build_scheduler,
    create_flashcard_review,
    study_class_expr,
    undo_last_review,
    utc_datetime,
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
    display_timezone = ZoneInfo("America/Los_Angeles")
    scheduler = build_scheduler(settings)

    def local_datetime(value: datetime | None) -> str:
        if value is None:
            return "unknown"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(display_timezone).strftime("%Y-%m-%d %I:%M:%S %p %Z")

    templates.env.filters["local_datetime"] = local_datetime

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

    def active_courses_query():
        return (
            select(Course)
            .where(Course.archived_at.is_(None))
            .order_by(Course.class_title.asc(), Course.title.asc(), Course.created_at.asc())
        )

    def active_lessons_query(course_id: int):
        return (
            select(Lesson)
            .where(Lesson.course_id == course_id, Lesson.archived_at.is_(None))
            .order_by(Lesson.created_at.asc())
        )

    def grouped_courses(session: Session) -> list[dict]:
        courses = session.scalars(active_courses_query()).all()
        return [{"course": course, "lessons": session.scalars(active_lessons_query(course.id)).all()} for course in courses]

    def class_grouped_courses(course_groups: list[dict]) -> list[dict]:
        classes: dict[str, list[dict]] = {}
        for group in course_groups:
            if not group["lessons"]:
                continue
            class_title = group["course"].class_title or group["course"].title
            classes.setdefault(class_title, []).append(group)
        return [{"class_title": class_title, "course_groups": groups} for class_title, groups in classes.items()]

    def course_summary(lessons: list[Lesson], lesson_statuses: dict[int, dict]) -> dict:
        counts = {"completed": 0, "active": 0, "failed": 0, "pending": 0}
        for lesson in lessons:
            state = lesson_statuses[lesson.id]["stage_state"]
            if state == "completed":
                counts["completed"] += 1
            elif state in {"running", "queued"}:
                counts["active"] += 1
            elif state == "failed":
                counts["failed"] += 1
            else:
                counts["pending"] += 1
        total = len(lessons)
        counts["total"] = total
        counts["percent"] = round(counts["completed"] * 100 / total) if total else 0
        return counts

    def legacy_media_by_course(session: Session, courses: list[Course]) -> dict[int, dict]:
        """Map course id -> video/flashcards generated for the course note before it became a course.

        Catalog restructures can promote a lesson note to a course note. The archived
        lesson row keeps the generated artifacts, and its trilium_note_id equals the
        new course's trilium_note_id, so the association is recomputed here instead of
        being stored — catalog syncs cannot invalidate it.
        """
        note_ids = [course.trilium_note_id for course in courses]
        if not note_ids:
            return {}
        legacy_lessons = session.scalars(
            select(Lesson).where(Lesson.archived_at.is_not(None), Lesson.trilium_note_id.in_(note_ids))
        ).all()
        by_note: dict[str, dict] = {}
        for lesson in legacy_lessons:
            youtube_url = lesson.youtube_upload.video_url if lesson.youtube_upload else None
            flashcard_count = sum(1 for flashcard in lesson.flashcards if not flashcard.suspended)
            if not youtube_url and not flashcard_count:
                continue
            entry = {"youtube_url": youtube_url, "flashcard_count": flashcard_count, "lesson_title": lesson.title}
            existing = by_note.get(lesson.trilium_note_id)
            if existing is None or (youtube_url and not existing["youtube_url"]):
                by_note[lesson.trilium_note_id] = entry
        return {course.id: by_note[course.trilium_note_id] for course in courses if course.trilium_note_id in by_note}

    def pop_flash(request: Request) -> tuple[str | None, str | None]:
        flash_level = request.cookies.get("flash_level")
        flash_message = request.cookies.get("flash_message")
        return flash_level, flash_message

    def redirect_with_flash(url: str, message: str, level: str = "info") -> RedirectResponse:
        response = RedirectResponse(url=url, status_code=303)
        response.set_cookie("flash_level", level, max_age=60, httponly=True, samesite="lax")
        response.set_cookie("flash_message", message, max_age=60, httponly=True, samesite="lax")
        return response

    def scoped_flashcards(stmt, class_title: str | None):
        if class_title:
            stmt = (
                stmt.join(Lesson, Lesson.id == Flashcard.lesson_id)
                .join(Course, Course.id == Lesson.course_id)
                .where(study_class_expr == class_title)
            )
        return stmt

    def local_day_start(now: datetime) -> datetime:
        return (
            now.astimezone(display_timezone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )

    def new_cards_introduced_today(session: Session, now: datetime) -> int:
        # Joining on the card and requiring it to still be scheduled means a
        # deck reset (which nulls stability) refreshes today's allowance.
        day_start = local_day_start(now)
        return (
            session.scalar(
                select(func.count())
                .select_from(FlashcardReview)
                .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
                .where(
                    FlashcardReview.state_before == "new",
                    FlashcardReview.reviewed_at >= day_start,
                    Flashcard.stability.is_not(None),
                )
            )
            or 0
        )

    def new_card_allowance(session: Session, now: datetime, new_today: int | None = None) -> tuple[int, int]:
        """Return (new_today, remaining allowance) under the daily new-card cap."""
        if new_today is None:
            new_today = new_cards_introduced_today(session, now)
        return new_today, max(0, max(0, settings.daily_new_cards) - new_today)

    learn_ahead = timedelta(minutes=max(0, settings.learn_ahead_minutes))
    learning_states = Flashcard.fsrs_state.in_(["learning", "relearning"])

    def study_class_options(session: Session, now: datetime) -> list[dict]:
        rows = session.execute(
            select(
                study_class_expr.label("class_title"),
                func.count(Flashcard.id),
                func.sum(case(((Flashcard.due_at <= now) & Flashcard.stability.is_not(None), 1), else_=0)),
                func.sum(case((Flashcard.stability.is_(None), 1), else_=0)),
            )
            .select_from(Flashcard)
            .join(Lesson, Lesson.id == Flashcard.lesson_id)
            .join(Course, Course.id == Lesson.course_id)
            .where(Flashcard.suspended.is_(False))
            .group_by(study_class_expr)
            .order_by(study_class_expr)
        ).all()
        return [
            {"class_title": title, "total": total, "due": due or 0, "new": new or 0}
            for title, total, due, new in rows
        ]

    def study_stats_payload(
        session: Session, now: datetime, class_title: str | None = None, new_today: int | None = None
    ) -> dict:
        active_cards = Flashcard.suspended.is_(False)
        day_start = local_day_start(now)
        total_cards = session.scalar(scoped_flashcards(select(func.count()).select_from(Flashcard), class_title).where(active_cards)) or 0
        review_due = (
            session.scalar(
                scoped_flashcards(select(func.count()).select_from(Flashcard), class_title).where(
                    active_cards, Flashcard.due_at <= now, Flashcard.stability.is_not(None)
                )
            )
            or 0
        )
        new_available = (
            session.scalar(
                scoped_flashcards(select(func.count()).select_from(Flashcard), class_title).where(
                    active_cards, Flashcard.stability.is_(None)
                )
            )
            or 0
        )
        new_today, new_allowance = new_card_allowance(session, now, new_today)
        new_remaining = min(new_available, new_allowance)
        due_count = review_due + new_remaining

        next_scheduled_due = session.scalar(
            scoped_flashcards(select(func.min(Flashcard.due_at)), class_title).where(
                active_cards, Flashcard.stability.is_not(None), Flashcard.due_at > now
            )
        )
        next_due_seconds = None
        if next_scheduled_due is not None:
            next_due_seconds = max(0, int((utc_datetime(next_scheduled_due) - now).total_seconds()))

        def review_count(*extra_clauses) -> int:
            stmt = (
                select(func.count())
                .select_from(FlashcardReview)
                .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
                .where(active_cards, FlashcardReview.reviewed_at >= day_start, *extra_clauses)
            )
            return session.scalar(scoped_flashcards(stmt, class_title)) or 0

        reviewed_today = review_count()
        daily_goal = max(1, settings.daily_review_goal)
        return {
            "total_cards": total_cards,
            "due_now": due_count,
            "remaining_after_current": max(due_count - 1, 0),
            "reviewed_today": reviewed_today,
            "passed_today": review_count(FlashcardReview.result != "again"),
            "failed_today": review_count(FlashcardReview.result == "again"),
            "daily_goal": daily_goal,
            "goal_reached": reviewed_today >= daily_goal,
            "new_today": new_today,
            "daily_new_cards": max(0, settings.daily_new_cards),
            "new_remaining": new_remaining,
            "next_due_seconds": next_due_seconds,
        }

    def ordered_flashcards(session: Session, class_title: str | None = None) -> list[Flashcard]:
        stmt = scoped_flashcards(select(Flashcard), class_title).where(Flashcard.suspended.is_(False))
        return session.scalars(stmt.order_by(Flashcard.due_at.asc(), Flashcard.id.asc())).all()

    def next_due_flashcard(
        session: Session, now: datetime, class_title: str | None = None, new_today: int | None = None
    ) -> Flashcard | None:
        # Serve the queue lesson by lesson (oldest backlog first) so a study
        # session stays on one topic instead of hopping between lessons.
        def lesson_grouped(extra_where, backlog_key) -> Flashcard | None:
            lesson_backlog = (
                select(Flashcard.lesson_id, func.min(backlog_key).label("lesson_due"))
                .where(Flashcard.suspended.is_(False), *extra_where)
                .group_by(Flashcard.lesson_id)
                .subquery()
            )
            stmt = (
                scoped_flashcards(select(Flashcard), class_title)
                .join(lesson_backlog, lesson_backlog.c.lesson_id == Flashcard.lesson_id)
                .where(Flashcard.suspended.is_(False), *extra_where)
                .order_by(lesson_backlog.c.lesson_due.asc(), Flashcard.lesson_id.asc(), Flashcard.due_at.asc(), Flashcard.id.asc())
            )
            return session.scalar(stmt)

        review_card = lesson_grouped(
            (Flashcard.due_at <= now, Flashcard.stability.is_not(None)), Flashcard.due_at
        )
        if review_card is not None:
            return review_card

        _, allowance = new_card_allowance(session, now, new_today)
        if allowance > 0:
            new_card = lesson_grouped((Flashcard.stability.is_(None),), Flashcard.id)
            if new_card is not None:
                return new_card

        # Learn-ahead: with nothing else to do, serve learning/relearning steps
        # a little early instead of stranding them minutes away.
        if learn_ahead > timedelta(0):
            return lesson_grouped(
                (Flashcard.due_at <= now + learn_ahead, learning_states), Flashcard.due_at
            )
        return None

    def study_flashcard_payload(flashcard: Flashcard | None) -> dict | None:
        if flashcard is None:
            return None
        trilium_note_url = None
        if flashcard.lesson:
            trilium_note_url = f"{settings.trilium_url.rstrip('/')}/#root/{flashcard.lesson.trilium_note_id}"
        return {
            "id": flashcard.id,
            "prompt": flashcard.prompt,
            "answer": flashcard.answer,
            "source_excerpt": flashcard.source_excerpt,
            "lesson_id": flashcard.lesson_id,
            "lesson_title": flashcard.lesson.title if flashcard.lesson else None,
            "trilium_note_url": trilium_note_url,
            "review_url": f"/study/{flashcard.id}/review",
            "suspend_url": f"/study/{flashcard.id}/suspend",
            "unsuspend_url": f"/study/{flashcard.id}/unsuspend",
            "edit_url": f"/study/{flashcard.id}/edit",
            "delete_url": f"/study/{flashcard.id}/delete",
            "suspended": flashcard.suspended,
        }

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

    def study_class_query(class_title: str | None) -> str:
        return f"?class={quote(class_title)}" if class_title else ""

    def study_response(
        request: Request,
        flashcard: Flashcard | None,
        stats: dict[str, int],
        browse_state: dict | None,
        review_mode: bool,
        flash_level: str | None,
        flash_message: str | None,
        class_options: list[dict],
        selected_class: str | None,
        analytics: dict,
    ) -> HTMLResponse:
        trilium_note_url = None
        if flashcard and flashcard.lesson:
            trilium_note_url = f"{settings.trilium_url.rstrip('/')}/#root/{flashcard.lesson.trilium_note_id}"
        class_query = study_class_query(selected_class)
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
                "browse_entry_url": f"/study/browse{class_query}" if stats["total_cards"] else None,
                "flash_level": flash_level,
                "flash_message": flash_message,
                "class_options": class_options,
                "selected_class": selected_class,
                "class_query": class_query,
                "analytics": analytics,
            },
        )
        if flash_message:
            response.delete_cookie("flash_level")
            response.delete_cookie("flash_message")
        return response

    def study_analytics_payload(session: Session, now: datetime) -> dict:
        today_local = now.astimezone(display_timezone).date()

        def review_counts_between(start_utc: datetime, end_utc: datetime | None = None) -> dict[date, int]:
            stmt = select(FlashcardReview.reviewed_at).where(FlashcardReview.reviewed_at >= start_utc)
            if end_utc is not None:
                stmt = stmt.where(FlashcardReview.reviewed_at < end_utc)
            counts: dict[date, int] = {}
            for ts in session.scalars(stmt):
                day = utc_datetime(ts).astimezone(display_timezone).date()
                counts[day] = counts.get(day, 0) + 1
            return counts

        # One 91-day window feeds both the heatmap and the streak; the streak
        # only fetches further windows when it actually extends past this one.
        heatmap_start_local = today_local - timedelta(days=90)
        heatmap_start_utc = local_day_start(now) - timedelta(days=90)
        day_counts = review_counts_between(heatmap_start_utc)
        heatmap_counts = dict(day_counts)
        fetched_start_local = heatmap_start_local
        fetched_start_utc = heatmap_start_utc

        def count_for(day: date) -> int:
            nonlocal fetched_start_local, fetched_start_utc
            while day < fetched_start_local:
                older_start_utc = fetched_start_utc - timedelta(days=91)
                day_counts.update(review_counts_between(older_start_utc, fetched_start_utc))
                fetched_start_utc = older_start_utc
                fetched_start_local -= timedelta(days=91)
            return day_counts.get(day, 0)

        cursor = today_local if count_for(today_local) else today_local - timedelta(days=1)
        streak = 0
        while count_for(cursor):
            streak += 1
            cursor -= timedelta(days=1)

        retention_window_start = now - timedelta(days=30)
        review_results = session.scalars(
            select(FlashcardReview.result).where(
                FlashcardReview.state_before == "review", FlashcardReview.reviewed_at >= retention_window_start
            )
        ).all()
        retention_total = len(review_results)
        retention_percent = (
            round(100 * (1 - sum(1 for result in review_results if result == "again") / retention_total))
            if retention_total
            else None
        )

        forecast_buckets = {today_local + timedelta(days=offset): 0 for offset in range(1, 8)}
        upcoming_due = session.scalars(
            select(Flashcard.due_at).where(
                Flashcard.suspended.is_(False),
                Flashcard.stability.is_not(None),
                Flashcard.due_at > now,
                Flashcard.due_at < now + timedelta(days=9),
            )
        ).all()
        for due_at in upcoming_due:
            due_date = utc_datetime(due_at).astimezone(display_timezone).date()
            if due_date in forecast_buckets:
                forecast_buckets[due_date] += 1
        forecast = [
            {"label": day.strftime("%a"), "date": day.isoformat(), "count": count}
            for day, count in sorted(forecast_buckets.items())
        ]

        def heat_level(count: int) -> int:
            if count == 0:
                return 0
            if count < 5:
                return 1
            if count < 15:
                return 2
            if count < 30:
                return 3
            return 4

        heatmap = []
        day = heatmap_start_local
        while day <= today_local:
            count = heatmap_counts.get(day, 0)
            heatmap.append({"date": day.isoformat(), "count": count, "level": heat_level(count)})
            day += timedelta(days=1)

        return {
            "streak": streak,
            "retention_percent": retention_percent,
            "forecast": forecast,
            "heatmap": heatmap,
        }

    def study_review_payload(session: Session, now: datetime, class_title: str | None = None) -> dict:
        new_today = new_cards_introduced_today(session, now)
        stats = study_stats_payload(session, now, class_title, new_today=new_today)
        next_card = next_due_flashcard(session, now, class_title, new_today=new_today)
        return {
            "stats": stats,
            "flashcard": study_flashcard_payload(next_card),
            "browse_entry_url": f"/study/browse{study_class_query(class_title)}" if stats["total_cards"] else None,
            "class_options": study_class_options(session, now),
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, db: Session = Depends(get_db)):
        course_groups = grouped_courses(db)
        lessons = [lesson for group in course_groups for lesson in group["lessons"]]
        jobs = db.scalars(select(JobEvent).order_by(JobEvent.created_at.desc()).limit(25)).all()
        queue_map = queue_positions(db)
        lesson_statuses = {lesson.id: lesson_status_payload(db, lesson, queue_map) for lesson in lessons}
        for group in course_groups:
            group["summary"] = course_summary(group["lessons"], lesson_statuses)
        legacy_media = legacy_media_by_course(db, [group["course"] for group in course_groups])
        class_groups = class_grouped_courses(course_groups)
        flash_level, flash_message = pop_flash(request)
        checks = runtime_checks(settings)
        failing_runtime_checks = [check for check in checks if not check.ok]
        response = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "catalog_root_id": settings.trilium_parent_note_id,
                "course_groups": course_groups,
                "class_groups": class_groups,
                "legacy_media": legacy_media,
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
        parent_note_id = (parent_note_id or settings.trilium_parent_note_id).strip()
        try:
            course_count, lesson_count, archived_course_count, archived_lesson_count = await sync_catalog(
                settings, db, parent_note_id
            )
        except TriliumClientError as exc:
            return redirect_with_flash("/", str(exc), level="error")
        db.commit()
        if course_count == 0:
            return redirect_with_flash("/", f"No direct child courses found under catalog note {parent_note_id}.")
        archive_note = ""
        if archived_course_count or archived_lesson_count:
            archive_note = f" Archived {archived_course_count} course(s) and {archived_lesson_count} lesson(s)."
        return redirect_with_flash(
            "/",
            f"Discovered {course_count} course(s) and {lesson_count} lesson(s) under catalog note {parent_note_id}.{archive_note}",
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    async def course_detail(request: Request, course_id: int, db: Session = Depends(get_db)):
        course = db.get(Course, course_id)
        if course is None or course.archived_at is not None:
            raise HTTPException(status_code=404, detail="Course not found")
        lessons = db.scalars(active_lessons_query(course.id)).all()
        queue_map = queue_positions(db)
        lesson_statuses = {lesson.id: lesson_status_payload(db, lesson, queue_map) for lesson in lessons}
        flash_level, flash_message = pop_flash(request)
        response = templates.TemplateResponse(
            request,
            "course_detail.html",
            {
                "request": request,
                "course": course,
                "lessons": lessons,
                "lesson_statuses": lesson_statuses,
                "summary": course_summary(lessons, lesson_statuses),
                "legacy_media": legacy_media_by_course(db, [course]).get(course.id),
                "flash_level": flash_level,
                "flash_message": flash_message,
            },
        )
        if flash_message:
            response.delete_cookie("flash_level")
            response.delete_cookie("flash_message")
        return response

    @app.post("/courses/generate")
    async def generate_selected_lessons(
        request: Request,
        lesson_ids: list[int] = Form(default=[]),
        force_regenerate: bool = Form(default=False),
        db: Session = Depends(get_db),
    ):
        redirect_url = request.headers.get("referer") or "/"
        selected = [db.get(Lesson, lesson_id) for lesson_id in lesson_ids]
        selected_lessons = [lesson for lesson in selected if lesson is not None and lesson.archived_at is None]
        if not selected_lessons:
            return redirect_with_flash(redirect_url, "No active lessons selected for generation.")
        for lesson in selected_lessons:
            runner.create_lesson_job(lesson.course_id, lesson.id, force_regenerate=force_regenerate)
        return redirect_with_flash(redirect_url, f"Queued {len(selected_lessons)} lesson generation job(s).")

    @app.post("/lessons/{lesson_id}/run")
    async def run_lesson(
        request: Request,
        lesson_id: int,
        force_regenerate: bool = Form(default=False),
        db: Session = Depends(get_db),
    ):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        redirect_url = request.headers.get("referer") or f"/courses/{lesson.course_id}"
        if lesson.archived_at is not None:
            return redirect_with_flash(redirect_url, f"Archived lesson '{lesson.title}' cannot be generated.", level="error")
        runner.create_lesson_job(lesson.course_id, lesson.id, force_regenerate=force_regenerate)
        return redirect_with_flash(redirect_url, f"Queued lesson '{lesson.title}' for generation.")

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
        queue_map = queue_positions(db)
        course_payloads = []
        for group in grouped_courses(db):
            course = group["course"]
            course_payloads.append(
                {
                    "id": course.id,
                    "title": course.title,
                    "trilium_note_id": course.trilium_note_id,
                    "lessons": [lesson_status_payload(db, lesson, queue_map) for lesson in group["lessons"]],
                }
            )
        return {
            "catalog_root_id": settings.trilium_parent_note_id,
            "courses": course_payloads,
            "runtime_checks": [check.__dict__ for check in runtime_checks(settings)],
        }

    @app.get("/api/courses/{course_id}")
    async def course_api(course_id: int, db: Session = Depends(get_db)):
        course = db.get(Course, course_id)
        if course is None or course.archived_at is not None:
            raise HTTPException(status_code=404, detail="Course not found")
        queue_map = queue_positions(db)
        return {
            "id": course.id,
            "title": course.title,
            "trilium_note_id": course.trilium_note_id,
            "lessons": [lesson_status_payload(db, lesson, queue_map) for lesson in db.scalars(active_lessons_query(course.id))],
        }

    def requested_class(request: Request) -> str | None:
        value = (request.query_params.get("class") or "").strip()
        return value or None

    @app.get("/study", response_class=HTMLResponse)
    async def study(request: Request, db: Session = Depends(get_db)):
        now = datetime.now(timezone.utc)
        flash_level, flash_message = pop_flash(request)
        selected_class = requested_class(request)
        stats = study_stats_payload(db, now, selected_class)
        flashcard = next_due_flashcard(db, now, selected_class)
        class_options = study_class_options(db, now)
        analytics = study_analytics_payload(db, now)
        return study_response(
            request, flashcard, stats, None, True, flash_level, flash_message, class_options, selected_class, analytics
        )

    @app.get("/study/browse", response_class=HTMLResponse)
    @app.get("/study/browse/{flashcard_id}", response_class=HTMLResponse)
    async def study_browse(request: Request, flashcard_id: int | None = None, db: Session = Depends(get_db)):
        now = datetime.now(timezone.utc)
        flash_level, flash_message = pop_flash(request)
        selected_class = requested_class(request)
        stats = study_stats_payload(db, now, selected_class)
        state = browse_payload(ordered_flashcards(db, selected_class), flashcard_id)
        flashcard = state["flashcard"] if state else None
        class_options = study_class_options(db, now)
        analytics = study_analytics_payload(db, now)
        return study_response(
            request, flashcard, stats, state, False, flash_level, flash_message, class_options, selected_class, analytics
        )

    @app.post("/study/{flashcard_id}/review")
    async def review_flashcard(
        request: Request,
        flashcard_id: int,
        result: str = Form(),
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        now = datetime.now(timezone.utc)
        is_new = flashcard.stability is None
        _, allowance = new_card_allowance(db, now)
        new_allowance_exhausted = is_new and allowance <= 0
        # Learning/relearning steps may be graded a little early (learn-ahead).
        # Because those steps are minutes long — shorter than the learn-ahead
        # window itself — a card's due_at stays inside the horizon right after
        # it's graded, so the horizon alone can't catch a double-submit; also
        # require this card not to have just been reviewed a moment ago.
        due_horizon = now + learn_ahead if flashcard.fsrs_state in {"learning", "relearning"} else now
        just_reviewed = flashcard.last_reviewed_at is not None and now - utc_datetime(flashcard.last_reviewed_at) < timedelta(
            seconds=5
        )
        if (
            flashcard.suspended
            or (not is_new and utc_datetime(flashcard.due_at) > due_horizon)
            or new_allowance_exhausted
            or just_reviewed
        ):
            if "application/json" in request.headers.get("accept", ""):
                return study_review_payload(db, now, selected_class)
            return redirect_with_flash(study_url, "That card was already reviewed.", "info")
        grade = result if result in {"again", "hard", "pass", "easy"} else "again"
        outcome = apply_review(scheduler, flashcard, grade, now)
        review = create_flashcard_review(flashcard, outcome)
        db.add(review)
        db.commit()
        if "application/json" in request.headers.get("accept", ""):
            return study_review_payload(db, datetime.now(timezone.utc), selected_class)
        return RedirectResponse(url=study_url, status_code=303)

    @app.post("/study/reset")
    async def reset_flashcards(class_title: str = Form(default=""), db: Session = Depends(get_db)):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        stmt = scoped_flashcards(select(Flashcard), selected_class).where(Flashcard.suspended.is_(False))
        flashcards = db.scalars(stmt).all()
        if not flashcards:
            return redirect_with_flash(study_url, "No flashcards available to reset.")
        now = datetime.now(timezone.utc)
        for flashcard in flashcards:
            flashcard.ease_factor = 2.5
            flashcard.interval_days = 0
            flashcard.repetitions = 0
            flashcard.due_at = now
            flashcard.suspended = False
            flashcard.stability = None
            flashcard.difficulty = None
            flashcard.fsrs_state = None
            flashcard.fsrs_step = None
            flashcard.last_reviewed_at = None
        db.commit()
        scope_note = f" in {selected_class}" if selected_class else ""
        return redirect_with_flash(
            study_url,
            f"Reset {len(flashcards)} flashcard(s){scope_note}. They'll re-enter as new cards, "
            f"limited to {max(0, settings.daily_new_cards)} per day.",
        )

    def tool_response(
        request: Request,
        db: Session,
        study_url: str,
        selected_class: str | None,
        message: str,
        level: str = "info",
    ):
        if "application/json" in request.headers.get("accept", ""):
            now = datetime.now(timezone.utc)
            payload = study_review_payload(db, now, selected_class)
            payload["message"] = message
            payload["level"] = level
            return payload
        return redirect_with_flash(study_url, message, level)

    @app.post("/study/{flashcard_id}/suspend")
    async def suspend_flashcard(
        request: Request,
        flashcard_id: int,
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        flashcard.suspended = True
        db.commit()
        return tool_response(request, db, study_url, selected_class, "Card suspended.")

    @app.post("/study/{flashcard_id}/unsuspend")
    async def unsuspend_flashcard(
        request: Request,
        flashcard_id: int,
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        flashcard.suspended = False
        db.commit()
        return tool_response(request, db, study_url, selected_class, "Card unsuspended.")

    @app.post("/study/{flashcard_id}/edit")
    async def edit_flashcard(
        request: Request,
        flashcard_id: int,
        prompt: str = Form(),
        answer: str = Form(),
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        prompt = prompt.strip()
        answer = answer.strip()
        if not prompt or not answer:
            return tool_response(request, db, study_url, selected_class, "Prompt and answer cannot be empty.", "error")
        flashcard.prompt = prompt
        flashcard.answer = answer
        db.commit()
        return tool_response(request, db, study_url, selected_class, "Card updated.")

    @app.post("/study/{flashcard_id}/delete")
    async def delete_flashcard(
        request: Request,
        flashcard_id: int,
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        flashcard = db.get(Flashcard, flashcard_id)
        if flashcard is None:
            raise HTTPException(status_code=404, detail="Flashcard not found")
        db.delete(flashcard)
        db.commit()
        return tool_response(request, db, study_url, selected_class, "Card deleted.")

    @app.post("/study/undo")
    async def undo_review(
        request: Request,
        class_title: str = Form(default=""),
        db: Session = Depends(get_db),
    ):
        selected_class = class_title.strip() or None
        study_url = f"/study{study_class_query(selected_class)}"
        restored, error = undo_last_review(db, selected_class)
        if error:
            return tool_response(request, db, study_url, selected_class, error, "error")
        db.commit()
        if "application/json" in request.headers.get("accept", ""):
            now = datetime.now(timezone.utc)
            payload = study_review_payload(db, now, selected_class)
            payload["flashcard"] = study_flashcard_payload(restored)
            payload["message"] = "Last review undone."
            payload["level"] = "info"
            return payload
        return redirect_with_flash(study_url, "Last review undone.")

    @app.get("/api/lessons/{lesson_id}")
    async def lesson_api(lesson_id: int, db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return lesson_status_payload(db, lesson, queue_positions(db))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    def api_or_redirect(
        request: Request, redirect_url: str, message: str, level: str = "info", status_code: int = 200
    ):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"level": level, "message": message}, status_code=status_code)
        return redirect_with_flash(redirect_url, message, level)

    @app.post("/lessons/{lesson_id}/queue-audio")
    async def queue_audio_lesson(lesson_id: int, request: Request, db: Session = Depends(get_db)):
        lesson = db.get(Lesson, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        redirect_url = request.headers.get("referer") or "/"
        youtube_url = lesson.youtube_upload.video_url if lesson.youtube_upload else None
        if not youtube_url:
            return api_or_redirect(
                request, redirect_url, f"{lesson.title} does not have a YouTube upload yet.", "error", status_code=409
            )
        try:
            await enqueue_audio_stream(settings.audio_queue_url, youtube_url)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip() or f"HTTP {exc.response.status_code}"
            return api_or_redirect(
                request, redirect_url, f"Audio queue rejected {lesson.title}: {detail}", "error", status_code=502
            )
        except httpx.HTTPError as exc:
            return api_or_redirect(
                request, redirect_url, f"Audio queue is unavailable for {lesson.title}: {exc}", "error", status_code=503
            )
        return api_or_redirect(request, redirect_url, f"Queued {lesson.title} for the audio YouTube stream.")

    @app.post("/courses/{course_id}/queue-audio-all")
    async def queue_audio_all(course_id: int, request: Request, db: Session = Depends(get_db)):
        course = db.get(Course, course_id)
        if course is None or course.archived_at is not None:
            raise HTTPException(status_code=404, detail="Course not found")
        redirect_url = request.headers.get("referer") or f"/courses/{course_id}"
        lessons = db.scalars(active_lessons_query(course.id)).all()
        queueable = [lesson for lesson in lessons if lesson.youtube_upload]
        if not queueable:
            return api_or_redirect(
                request, redirect_url, "No lessons in this course have a YouTube upload yet.", "error", status_code=409
            )
        queued = 0
        failures: list[str] = []
        for lesson in queueable:
            try:
                await enqueue_audio_stream(settings.audio_queue_url, lesson.youtube_upload.video_url)
                queued += 1
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text.strip() or f"HTTP {exc.response.status_code}"
                failures.append(f"{lesson.title} ({detail})")
            except httpx.HTTPError as exc:
                failures.append(f"{lesson.title} ({exc})")
        if failures:
            level = "error" if queued == 0 else "info"
            status_code = 502 if queued == 0 else 200
            message = f"Queued {queued} of {len(queueable)} lesson(s). Failed: {', '.join(failures)}"
        else:
            level = "info"
            status_code = 200
            message = f"Queued {queued} lesson(s) for the audio YouTube stream."
        return api_or_redirect(request, redirect_url, message, level, status_code=status_code)

    return app


try:
    app = create_app()
except Exception as exc:  # pragma: no cover
    fallback = FastAPI(title="Trilium Study (configuration error)")

    @fallback.get("/healthz")
    async def healthz():
        return {"ok": False, "error": str(exc)}

    app = fallback
