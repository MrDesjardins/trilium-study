from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.bootstrap import bootstrap_database
from app.catalog import sync_catalog
from app.config import Settings, get_settings
from app.content import LessonCollector, TriliumClient, TriliumClientError
from app.db import make_session_factory
from app.jobs import JobRunner, PipelineServices
from app.logging_utils import configure_logging
from app.models import Course, Lesson
from app.pipeline import (
    CommandTTSGenerator,
    DefaultFlashcardGenerator,
    DefaultScriptGenerator,
    FfmpegVideoRenderer,
    YouTubeApiPublisher,
)
from app.storage import lesson_workspace


def services_factory(settings: Settings) -> PipelineServices:
    trilium_client = TriliumClient(settings.trilium_url, settings.trilium_etapi_token)
    return PipelineServices(
        collector=LessonCollector(trilium_client),
        script_generator=DefaultScriptGenerator(settings),
        flashcard_generator=DefaultFlashcardGenerator(settings),
        tts_generator=CommandTTSGenerator(settings),
        video_renderer=FfmpegVideoRenderer(settings),
        youtube_publisher=YouTubeApiPublisher(settings),
    )


def lesson_workspace_relpaths(settings: Settings, lesson_ids: list[int]) -> list[str]:
    session_factory, _ = make_session_factory(settings)
    bootstrap_database(settings)
    paths: list[str] = []
    with session_factory() as session:
        for lesson_id in lesson_ids:
            lesson = session.get(Lesson, lesson_id)
            if lesson is None:
                raise SystemExit(f"Lesson {lesson_id} not found in database")
            course = session.get(Course, lesson.course_id)
            if course is None:
                raise SystemExit(f"Course for lesson {lesson_id} not found in database")
            workspace = lesson_workspace(settings.workspace_path, course, lesson)
            paths.append(str(workspace.relative_to(settings.workspace_path)))
    return paths


def list_lessons(settings: Settings, *, include_archived: bool = False) -> None:
    session_factory, _ = make_session_factory(settings)
    bootstrap_database(settings)
    with session_factory() as session:
        query = (
            select(Lesson.id, Course.title, Lesson.title, Lesson.stage, Lesson.stage_state, Lesson.archived_at)
            .join(Course, Course.id == Lesson.course_id)
            .order_by(Course.title, Lesson.id)
        )
        if not include_archived:
            query = query.where(Lesson.archived_at.is_(None), Course.archived_at.is_(None))
        rows = session.execute(query).all()
    if not rows:
        print("No lessons found.")
        return
    print(f"{'ID':>6}  {'Stage':<12}  {'State':<12}  {'Archived':<8}  Course / Title")
    print("-" * 100)
    for lesson_id, course_title, title, stage, stage_state, archived_at in rows:
        archived = "yes" if archived_at else "no"
        print(f"{lesson_id:>6}  {stage:<12}  {stage_state:<12}  {archived:<8}  {course_title} / {title}")


def sync_catalog_cli(settings: Settings) -> None:
    parent_note_id = (settings.trilium_parent_note_id or "").strip()
    if not parent_note_id:
        raise SystemExit("TRILIUM_PARENT_NOTE_ID is not configured")
    bootstrap_database(settings)
    session_factory, _ = make_session_factory(settings)
    with session_factory() as session:
        active_before = dict(
            session.execute(select(Course.id, Course.title).where(Course.archived_at.is_(None))).all()
        )
        try:
            course_count, lesson_count, archived_course_count, archived_lesson_count = asyncio.run(
                sync_catalog(settings, session, parent_note_id)
            )
        except TriliumClientError as exc:
            raise SystemExit(f"Catalog sync failed: {exc}")
        session.commit()
        newly_archived = session.execute(
            select(Course.id, Course.title).where(
                Course.id.in_(active_before), Course.archived_at.is_not(None)
            )
        ).all()
    print(
        f"Synced {course_count} course(s) and {lesson_count} lesson(s) under catalog note {parent_note_id}; "
        f"archived {archived_course_count} course(s) and {archived_lesson_count} lesson(s)."
    )
    if newly_archived:
        print(
            "Warning: this sync archived previously active course(s). If unexpected, "
            "check that TRILIUM_PARENT_NOTE_ID points to the correct catalog note.",
            file=sys.stderr,
        )
        for course_id, title in newly_archived:
            print(f"  archived course {course_id}: {title}", file=sys.stderr)
    print(f"SYNC_ARCHIVED_COURSES {len(newly_archived)}", flush=True)


def list_courses(settings: Settings) -> None:
    bootstrap_database(settings)
    session_factory, _ = make_session_factory(settings)
    pending_lessons = (
        select(Lesson.course_id, func.count(Lesson.id).label("pending"))
        .where(Lesson.archived_at.is_(None), Lesson.stage_state != "completed")
        .group_by(Lesson.course_id)
        .subquery()
    )
    with session_factory() as session:
        rows = session.execute(
            select(Course.id, Course.class_title, Course.title, Course.archived_at, func.coalesce(pending_lessons.c.pending, 0))
            .outerjoin(pending_lessons, pending_lessons.c.course_id == Course.id)
            .order_by(Course.class_title, Course.title, Course.id)
        ).all()
    if not rows:
        print("No courses found.")
        return
    print(f"{'ID':>6}  {'Pending':>7}  {'Archived':<8}  Class / Title")
    print("-" * 100)
    for course_id, class_title, title, archived_at, pending in rows:
        archived = "yes" if archived_at else "no"
        label = f"{class_title} / {title}" if class_title else title
        print(f"{course_id:>6}  {pending:>7}  {archived:<8}  {label}")


def pending_lesson_ids_for_course(settings: Settings, course_id: int) -> list[int]:
    bootstrap_database(settings)
    session_factory, _ = make_session_factory(settings)
    with session_factory() as session:
        course = session.get(Course, course_id)
        if course is None:
            raise SystemExit(f"Course {course_id} not found in database")
        if course.archived_at is not None:
            raise SystemExit(f"Course {course_id} is archived and cannot be generated")
        return list(
            session.scalars(
                select(Lesson.id)
                .where(
                    Lesson.course_id == course_id,
                    Lesson.archived_at.is_(None),
                    Lesson.stage_state != "completed",
                )
                .order_by(Lesson.created_at.asc())
            ).all()
        )


def run_lessons(settings: Settings, lesson_ids: list[int], *, force_regenerate: bool = False) -> None:
    configure_logging(settings)
    bootstrap_database(settings)
    session_factory, _ = make_session_factory(settings)
    runner = JobRunner(settings, session_factory, lambda: services_factory(settings))

    completed: list[int] = []
    failed: list[int] = []
    for lesson_id in lesson_ids:
        with session_factory() as session:
            lesson = session.get(Lesson, lesson_id)
            if lesson is None:
                raise SystemExit(f"Lesson {lesson_id} not found in database")
            if lesson.archived_at is not None:
                raise SystemExit(f"Lesson {lesson_id} is archived and cannot be generated")
            title = lesson.title
            course_id = lesson.course_id

        print(f"Generating lesson {lesson_id}: {title}", flush=True)
        job_id = runner.create_lesson_job(course_id, lesson_id, force_regenerate=force_regenerate)
        try:
            runner._run_job(job_id)
        except Exception as exc:
            print(f"Lesson {lesson_id} failed: {exc}", file=sys.stderr, flush=True)
            failed.append(lesson_id)
            print(f"LESSON_RESULT {lesson_id} failed", flush=True)
            continue

        with session_factory() as session:
            lesson = session.get(Lesson, lesson_id)
            if lesson is None or lesson.stage_state != "completed":
                failed.append(lesson_id)
                print(f"LESSON_RESULT {lesson_id} failed", flush=True)
                continue
        completed.append(lesson_id)
        print(f"Lesson {lesson_id} completed.", flush=True)
        print(f"LESSON_RESULT {lesson_id} completed", flush=True)

    completed_note = ", ".join(map(str, completed)) if completed else "none"
    failed_note = ", ".join(map(str, failed)) if failed else "none"
    print(f"Summary: completed [{completed_note}]; failed [{failed_note}]", flush=True)
    if failed:
        print(f"Generation failed for lesson(s): {failed_note}", file=sys.stderr, flush=True)
        raise SystemExit(2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run lesson generation locally against the configured database and workspace."
    )
    parser.add_argument("--list", action="store_true", help="List lessons in the configured database.")
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived lessons when listing lessons.",
    )
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="List courses with their pending (not fully generated) lesson counts.",
    )
    parser.add_argument(
        "--sync-catalog",
        action="store_true",
        help="Sync courses and lessons from the Trilium catalog note into the database.",
    )
    parser.add_argument(
        "--course",
        type=int,
        metavar="COURSE_ID",
        help="Generate all pending (not fully generated) lessons of the given course.",
    )
    parser.add_argument(
        "--print-ids",
        action="store_true",
        help="With --course, print pending lesson IDs (one per line) instead of generating.",
    )
    parser.add_argument(
        "--workspace-dirs",
        nargs="+",
        type=int,
        metavar="LESSON_ID",
        help="Print workspace directory paths relative to WORKSPACE_DIR for the given lesson IDs.",
    )
    parser.add_argument(
        "--force-regenerate",
        action="store_true",
        help="Regenerate all pipeline stages even when cached artifacts exist.",
    )
    parser.add_argument("lesson_ids", nargs="*", type=int, help="Lesson IDs to generate.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()

    if args.list:
        list_lessons(settings, include_archived=args.include_archived)
        return

    if args.list_courses:
        list_courses(settings)
        return

    if args.sync_catalog:
        sync_catalog_cli(settings)
        return

    if args.workspace_dirs:
        for path in lesson_workspace_relpaths(settings, args.workspace_dirs):
            print(path)
        return

    if args.course is not None:
        if args.lesson_ids:
            raise SystemExit("Provide either --course or explicit lesson IDs, not both.")
        lesson_ids = pending_lesson_ids_for_course(settings, args.course)
        if args.print_ids:
            for lesson_id in lesson_ids:
                print(lesson_id)
            return
        if not lesson_ids:
            print(f"No pending lessons for course {args.course}.")
            return
        run_lessons(settings, lesson_ids, force_regenerate=args.force_regenerate)
        return

    if not args.lesson_ids:
        raise SystemExit("Provide lesson IDs to generate, or use --list / --list-courses / --course / --workspace-dirs.")

    run_lessons(settings, args.lesson_ids, force_regenerate=args.force_regenerate)


if __name__ == "__main__":
    main()
