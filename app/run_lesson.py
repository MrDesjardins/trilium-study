from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.bootstrap import bootstrap_database
from app.config import Settings, get_settings
from app.content import LessonCollector, TriliumClient
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


def run_lessons(settings: Settings, lesson_ids: list[int], *, force_regenerate: bool = False) -> None:
    configure_logging(settings)
    bootstrap_database(settings)
    session_factory, _ = make_session_factory(settings)
    runner = JobRunner(settings, session_factory, lambda: services_factory(settings))

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
            continue

        with session_factory() as session:
            lesson = session.get(Lesson, lesson_id)
            if lesson is None or lesson.stage_state != "completed":
                failed.append(lesson_id)
                continue
        print(f"Lesson {lesson_id} completed.", flush=True)

    if failed:
        raise SystemExit(f"Generation failed for lesson(s): {', '.join(map(str, failed))}")


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

    if args.workspace_dirs:
        for path in lesson_workspace_relpaths(settings, args.workspace_dirs):
            print(path)
        return

    if not args.lesson_ids:
        raise SystemExit("Provide lesson IDs to generate, or use --list / --workspace-dirs.")

    run_lessons(settings, args.lesson_ids, force_regenerate=args.force_regenerate)


if __name__ == "__main__":
    main()
