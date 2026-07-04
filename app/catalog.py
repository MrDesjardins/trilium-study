from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.content import TriliumClient
from app.models import Course, Lesson


async def sync_catalog(settings: Settings, session: Session, parent_note_id: str) -> tuple[int, int, int, int]:
    """Sync courses and lessons from the Trilium university note into the database.

    The hierarchy is: university (parent_note_id) -> class -> course -> lesson.
    Direct children of the university note are classes; each class's children are
    courses; each course's children are lessons. Classes without children (utility
    notes such as links) contribute no courses.

    Returns (course_count, active_lesson_count, archived_course_count, archived_lesson_count).
    The caller is responsible for committing the session.
    """
    client = TriliumClient(settings.trilium_url, settings.trilium_etapi_token)
    catalog_note = await client.get_note(parent_note_id)
    class_notes = await client.get_course_lessons(parent_note_id)
    course_entries: list[tuple[str, str, dict]] = []
    for class_note in class_notes:
        class_note_id = class_note["noteId"]
        class_title = class_note.get("title", class_note_id)
        for course_note in await client.get_course_lessons(class_note_id):
            course_entries.append((class_note_id, class_title, course_note))
    now = datetime.now(timezone.utc)
    active_course_note_ids: set[str] = set()
    active_lesson_count = 0
    archived_course_count = 0
    archived_lesson_count = 0

    for class_note_id, class_title, course_note in course_entries:
        course_note_id = course_note["noteId"]
        active_course_note_ids.add(course_note_id)
        course = session.scalar(select(Course).where(Course.trilium_note_id == course_note_id))
        if course is None:
            course = Course(
                trilium_note_id=course_note_id,
                title=course_note.get("title", course_note_id),
                class_title=class_title,
                parent_note_id=class_note_id,
                traversal_hash=course_note_id,
                last_synced_at=now,
            )
            session.add(course)
            session.flush()
        else:
            course.title = course_note.get("title", course.title)
            course.class_title = class_title
            course.parent_note_id = class_note_id
            course.traversal_hash = course_note_id
            course.last_synced_at = now
            course.archived_at = None

        lesson_notes = await client.get_course_lessons(course_note_id)
        active_lesson_note_ids: set[str] = set()
        for lesson_note in lesson_notes:
            lesson_note_id = lesson_note["noteId"]
            active_lesson_note_ids.add(lesson_note_id)
            active_lesson_count += 1
            lesson = session.scalar(
                select(Lesson).where(Lesson.course_id == course.id, Lesson.trilium_note_id == lesson_note_id)
            )
            if lesson is None:
                session.add(
                    Lesson(
                        course_id=course.id,
                        trilium_note_id=lesson_note_id,
                        title=lesson_note.get("title", lesson_note_id),
                        parent_note_id=course.trilium_note_id,
                        content_hash="",
                        stage="pending",
                        stage_state="pending",
                    )
                )
            else:
                lesson.title = lesson_note.get("title", lesson.title)
                lesson.parent_note_id = course.trilium_note_id
                lesson.archived_at = None

        for lesson in session.scalars(select(Lesson).where(Lesson.course_id == course.id)).all():
            if lesson.trilium_note_id not in active_lesson_note_ids and lesson.archived_at is None:
                lesson.archived_at = now
                archived_lesson_count += 1

    for course in session.scalars(select(Course)).all():
        if course.trilium_note_id not in active_course_note_ids and course.archived_at is None:
            course.archived_at = now
            archived_course_count += 1
            for lesson in course.lessons:
                if lesson.archived_at is None:
                    lesson.archived_at = now
                    archived_lesson_count += 1

    if not course_entries:
        # Validate the catalog note ID even when it has no courses.
        catalog_note.get("title")
    return len(course_entries), active_lesson_count, archived_course_count, archived_lesson_count
