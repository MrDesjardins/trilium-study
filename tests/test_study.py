from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import Base, make_session_factory
from app.main import create_app
from app.models import Course, Flashcard, FlashcardReview, Job, JobEvent, Lesson, YouTubeUpload


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


def seed_study_data(session_factory, *, now: datetime) -> dict[str, int]:
    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="hash")
        session.add(lesson)
        session.flush()

        due_card = Flashcard(
            lesson_id=lesson.id,
            prompt="Due card",
            answer="Answer 1",
            due_at=now - timedelta(hours=2),
            repetitions=2,
            interval_days=3,
        )
        future_card = Flashcard(
            lesson_id=lesson.id,
            prompt="Future card",
            answer="Answer 2",
            due_at=now + timedelta(days=3),
            repetitions=4,
            interval_days=10,
        )
        failed_due_card = Flashcard(
            lesson_id=lesson.id,
            prompt="Failed due card",
            answer="Answer 3",
            due_at=now - timedelta(minutes=10),
            repetitions=0,
            interval_days=1,
        )
        session.add_all([due_card, future_card, failed_due_card])
        session.flush()

        session.add_all(
            [
                FlashcardReview(
                    flashcard_id=due_card.id,
                    result="pass",
                    scheduled_due_at=now - timedelta(days=1),
                    reviewed_at=now,
                    next_due_at=now + timedelta(days=1),
                    ease_factor_after=2.6,
                    interval_days_after=1,
                    repetitions_after=1,
                ),
                FlashcardReview(
                    flashcard_id=failed_due_card.id,
                    result="again",
                    scheduled_due_at=now - timedelta(hours=3),
                    reviewed_at=now,
                    next_due_at=now + timedelta(days=1),
                    ease_factor_after=2.3,
                    interval_days_after=1,
                    repetitions_after=0,
                ),
            ]
        )
        session.commit()
        return {"due_card_id": due_card.id, "future_card_id": future_card.id, "failed_due_card_id": failed_due_card.id}


def test_study_page_shows_queue_stats_and_due_card(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/study")

    assert response.status_code == 200
    assert "Due now" in response.text
    assert "Left after this one" in response.text
    assert "Failed today" in response.text
    assert "Browse all cards" in response.text
    assert "Due card" in response.text
    assert ">2<" in response.text
    assert ">1<" in response.text
    assert ">3<" in response.text


def test_review_flashcard_returns_json_for_in_place_updates(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    ids = seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            f"/study/{ids['due_card_id']}/review",
            data={"result": "pass"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stats"]["reviewed_today"] >= 2
    assert payload["flashcard"] is not None
    assert payload["flashcard"]["id"] == ids["failed_due_card_id"]
    assert payload["browse_entry_url"] == "/study/browse"


def test_duplicate_review_post_does_not_grade_same_card_twice(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    ids = seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        first_response = client.post(
            f"/study/{ids['due_card_id']}/review",
            data={"result": "pass"},
            headers={"Accept": "application/json"},
        )
        second_response = client.post(
            f"/study/{ids['due_card_id']}/review",
            data={"result": "pass"},
            headers={"Accept": "application/json"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["flashcard"]["id"] == ids["failed_due_card_id"]
    with session_factory() as session:
        reviews = session.query(FlashcardReview).filter_by(flashcard_id=ids["due_card_id"]).all()
        assert len(reviews) == 2


def test_study_browse_mode_can_open_specific_card(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    ids = seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"/study/browse/{ids['future_card_id']}")

    assert response.status_code == 200
    assert "Browse all cards" in response.text
    assert "Card 3 of 3" in response.text
    assert "Future card" in response.text
    assert "Browse mode is read-only" in response.text


def test_reset_flashcards_requeues_all_cards(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    ids = seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/study/reset", follow_redirects=True)

    assert response.status_code == 200
    assert "Reset 3 flashcard(s) for a fresh study pass." in response.text

    with session_factory() as session:
        cards = session.query(Flashcard).order_by(Flashcard.id.asc()).all()
        assert len(cards) == 3
        for card in cards:
            assert card.repetitions == 0
            assert card.interval_days == 0
            assert card.ease_factor == 2.5
            due_at = card.due_at if card.due_at.tzinfo else card.due_at.replace(tzinfo=timezone.utc)
            assert due_at <= datetime.now(timezone.utc)
        assert {card.id for card in cards} == {ids["due_card_id"], ids["future_card_id"], ids["failed_due_card_id"]}


def test_queue_audio_lesson_posts_youtube_url(monkeypatch, tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="hash")
        session.add(lesson)
        session.flush()
        session.add(YouTubeUpload(lesson_id=lesson.id, video_id="abc123", video_url="https://www.youtube.com/watch?v=abc123"))
        session.commit()
        lesson_id = lesson.id

    captured: dict[str, str] = {}

    async def fake_enqueue(queue_url: str, youtube_url: str) -> None:
        captured["queue_url"] = queue_url
        captured["youtube_url"] = youtube_url

    monkeypatch.setattr("app.main.enqueue_audio_stream", fake_enqueue)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/lessons/{lesson_id}/queue-audio", headers={"referer": "/lessons/1"}, follow_redirects=True)

    assert response.status_code == 200
    assert "Queued Lesson 1 for the audio YouTube stream." in response.text
    assert captured == {
        "queue_url": "http://127.0.0.1:8000/queue/add",
        "youtube_url": "https://www.youtube.com/watch?v=abc123",
    }


def test_dashboard_shows_job_events_in_los_angeles_time(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        job = Job(course_id=course.id, lesson_id=None, job_type="course_sync", state="completed")
        session.add(job)
        session.flush()
        session.add(
            JobEvent(
                job_id=job.id,
                event_type="job_completed",
                message="Done",
                created_at=datetime(2026, 5, 28, 6, 30, tzinfo=timezone.utc),
            )
        )
        session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "2026-05-27 11:30:00 PM PDT" in response.text


def test_catalog_sync_creates_courses_and_archives_missing_state(monkeypatch, tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        old_course = Course(trilium_note_id="old-course", title="Old Course", parent_note_id=None, traversal_hash="old-course")
        session.add(old_course)
        session.flush()
        old_lesson = Lesson(
            course_id=old_course.id,
            trilium_note_id="old-lesson",
            title="Old Lesson",
            parent_note_id="old-course",
            content_hash="hash",
        )
        session.add(old_lesson)
        session.flush()
        session.add(Flashcard(lesson_id=old_lesson.id, prompt="Old Q", answer="Old A"))
        session.add(YouTubeUpload(lesson_id=old_lesson.id, video_id="old", video_url="https://youtube.com/watch?v=old"))
        session.commit()
        old_course_id = old_course.id
        old_lesson_id = old_lesson.id

    class FakeTriliumClient:
        def __init__(self, *_args):
            self.notes = {
                "catalog": {"noteId": "catalog", "title": "University", "childNoteIds": ["class-1", "links"]},
                "class-1": {"noteId": "class-1", "title": "Class 1", "childNoteIds": ["course-a", "course-b"]},
                "links": {"noteId": "links", "title": "Links", "childNoteIds": []},
                "course-a": {"noteId": "course-a", "title": "Course A", "childNoteIds": ["lesson-a1", "lesson-a2"]},
                "course-b": {"noteId": "course-b", "title": "Course B", "childNoteIds": ["lesson-b1"]},
                "lesson-a1": {"noteId": "lesson-a1", "title": "Lesson A1", "childNoteIds": []},
                "lesson-a2": {"noteId": "lesson-a2", "title": "Lesson A2", "childNoteIds": []},
                "lesson-b1": {"noteId": "lesson-b1", "title": "Lesson B1", "childNoteIds": []},
            }

        async def get_note(self, note_id: str):
            return self.notes[note_id]

        async def get_course_lessons(self, parent_note_id: str):
            note = self.notes[parent_note_id]
            return [self.notes[child_id] for child_id in note["childNoteIds"]]

    monkeypatch.setattr("app.catalog.TriliumClient", FakeTriliumClient)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/courses/sync", data={"parent_note_id": "catalog"}, follow_redirects=True)

    assert response.status_code == 200
    assert "Discovered 2 course(s) and 3 lesson(s)" in response.text
    with session_factory() as session:
        active_courses = session.scalars(select(Course).where(Course.archived_at.is_(None)).order_by(Course.trilium_note_id)).all()
        assert [course.trilium_note_id for course in active_courses] == ["course-a", "course-b"]
        assert [course.class_title for course in active_courses] == ["Class 1", "Class 1"]
        assert [course.parent_note_id for course in active_courses] == ["class-1", "class-1"]
        assert {lesson.trilium_note_id for lesson in session.scalars(select(Lesson).where(Lesson.archived_at.is_(None))).all()} == {
            "lesson-a1",
            "lesson-a2",
            "lesson-b1",
        }
        assert session.get(Course, old_course_id).archived_at is not None
        assert session.get(Lesson, old_lesson_id).archived_at is not None
        assert session.scalar(select(func.count()).select_from(Flashcard)) == 1
        assert session.scalar(select(func.count()).select_from(YouTubeUpload)) == 1


def test_dashboard_api_groups_courses_and_excludes_archived_lessons(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course-a", title="Course A", parent_note_id="catalog", traversal_hash="course-a")
        archived_course = Course(
            trilium_note_id="course-z",
            title="Course Z",
            parent_note_id="catalog",
            traversal_hash="course-z",
            archived_at=datetime.now(timezone.utc),
        )
        session.add_all([course, archived_course])
        session.flush()
        active_lesson = Lesson(course_id=course.id, trilium_note_id="lesson-a", title="Lesson A", parent_note_id="course-a", content_hash="")
        archived_lesson = Lesson(
            course_id=course.id,
            trilium_note_id="lesson-old",
            title="Old Lesson",
            parent_note_id="course-a",
            content_hash="",
            archived_at=datetime.now(timezone.utc),
        )
        hidden_lesson = Lesson(
            course_id=archived_course.id,
            trilium_note_id="lesson-z",
            title="Lesson Z",
            parent_note_id="course-z",
            content_hash="",
        )
        session.add_all([active_lesson, archived_lesson, hidden_lesson])
        session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert [course["title"] for course in payload["courses"]] == ["Course A"]
    assert [lesson["title"] for lesson in payload["courses"][0]["lessons"]] == ["Lesson A"]


def test_dashboard_links_to_course_pages_instead_of_inline_lessons(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course-a", title="Course A", parent_note_id="catalog", traversal_hash="course-a")
        session.add(course)
        session.flush()
        session.add(Lesson(course_id=course.id, trilium_note_id="lesson-a", title="Lesson A", parent_note_id="course-a", content_hash=""))
        session.commit()
        course_id = course.id

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert f'href="/courses/{course_id}"' in response.text
    assert "1 lesson" in response.text
    assert "Open lesson details" not in response.text


def test_course_page_shows_active_lessons_and_excludes_archived(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course-a", title="Course A", parent_note_id="catalog", traversal_hash="course-a")
        session.add(course)
        session.flush()
        active_lesson = Lesson(course_id=course.id, trilium_note_id="lesson-a", title="Lesson A", parent_note_id="course-a", content_hash="")
        archived_lesson = Lesson(
            course_id=course.id,
            trilium_note_id="lesson-old",
            title="Old Lesson",
            parent_note_id="course-a",
            content_hash="",
            archived_at=datetime.now(timezone.utc),
        )
        session.add_all([active_lesson, archived_lesson])
        session.commit()
        course_id = course.id

    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get(f"/courses/{course_id}")
        api = client.get(f"/api/courses/{course_id}")
        missing = client.get("/courses/9999")

    assert page.status_code == 200
    assert "Course A" in page.text
    assert "Lesson A" in page.text
    assert "Old Lesson" not in page.text
    assert "Open lesson details" in page.text
    assert api.status_code == 200
    assert [lesson["title"] for lesson in api.json()["lessons"]] == ["Lesson A"]
    assert missing.status_code == 404


def test_lesson_detail_page_shows_breadcrumb_to_course(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course-a", title="Course A", parent_note_id="catalog", traversal_hash="course-a")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-a", title="Lesson A", parent_note_id="course-a", content_hash="")
        session.add(lesson)
        session.commit()
        course_id = course.id
        lesson_id = lesson.id

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(f"/lessons/{lesson_id}")

    assert response.status_code == 200
    assert f'href="/courses/{course_id}"' in response.text
    assert "Course A" in response.text


def test_archived_course_page_returns_404(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(
            trilium_note_id="course-z",
            title="Course Z",
            parent_note_id="catalog",
            traversal_hash="course-z",
            archived_at=datetime.now(timezone.utc),
        )
        session.add(course)
        session.commit()
        course_id = course.id

    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get(f"/courses/{course_id}")
        api = client.get(f"/api/courses/{course_id}")

    assert page.status_code == 404
    assert api.status_code == 404


def test_archived_lesson_cannot_be_generated(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id="catalog", traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(
            course_id=course.id,
            trilium_note_id="lesson-1",
            title="Lesson 1",
            parent_note_id="course",
            content_hash="",
            archived_at=datetime.now(timezone.utc),
        )
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/lessons/{lesson_id}/run", follow_redirects=True)

    assert response.status_code == 200
    assert "Archived lesson" in response.text
    assert "cannot be generated." in response.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_queue_audio_lesson_requires_youtube_upload(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="hash")
        session.add(lesson)
        session.commit()
        lesson_id = lesson.id

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/lessons/{lesson_id}/queue-audio", headers={"referer": "/lessons/1"}, follow_redirects=True)

    assert response.status_code == 200
    assert "Lesson 1 does not have a YouTube upload yet." in response.text
