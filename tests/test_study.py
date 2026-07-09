from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import Base, make_session_factory
from app.main import create_app
from app.models import Course, Flashcard, FlashcardReview, Job, JobEvent, Lesson, YouTubeUpload


def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    data = {
        "TRILIUM_URL": "http://example",
        "TRILIUM_ETAPI_TOKEN": "token",
        "TRILIUM_PARENT_NOTE_ID": "course",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "WORKSPACE_DIR": str(tmp_path / "workspace"),
        "LOG_DIR": str(tmp_path / "logs"),
    }
    data.update(overrides)
    return Settings.model_validate(data)


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
            stability=5.0,
            difficulty=5.0,
            fsrs_state="review",
            last_reviewed_at=now - timedelta(days=3),
        )
        future_card = Flashcard(
            lesson_id=lesson.id,
            prompt="Future card",
            answer="Answer 2",
            due_at=now + timedelta(days=3),
            repetitions=4,
            interval_days=10,
            stability=10.0,
            difficulty=4.0,
            fsrs_state="review",
            last_reviewed_at=now - timedelta(days=7),
        )
        failed_due_card = Flashcard(
            lesson_id=lesson.id,
            prompt="Failed due card",
            answer="Answer 3",
            due_at=now - timedelta(minutes=10),
            repetitions=0,
            interval_days=1,
            stability=1.0,
            difficulty=6.0,
            fsrs_state="relearning",
            last_reviewed_at=now - timedelta(hours=3),
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
                    state_before="review",
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
                    state_before="review",
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
    assert "Reset 3 flashcard(s)." in response.text

    with session_factory() as session:
        cards = session.query(Flashcard).order_by(Flashcard.id.asc()).all()
        assert len(cards) == 3
        for card in cards:
            assert card.repetitions == 0
            assert card.interval_days == 0
            assert card.ease_factor == 2.5
            assert card.stability is None
            assert card.fsrs_state is None
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


def test_queue_audio_lesson_returns_json_without_redirect(monkeypatch, tmp_path: Path):
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

    async def fake_enqueue(queue_url: str, youtube_url: str) -> None:
        return None

    monkeypatch.setattr("app.main.enqueue_audio_stream", fake_enqueue)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/lessons/{lesson_id}/queue-audio", headers={"Accept": "application/json"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["level"] == "info"
    assert "Queued Lesson 1" in payload["message"]


def test_queue_audio_all_queues_every_lesson_with_a_youtube_upload(monkeypatch, tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        with_video = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="hash")
        also_with_video = Lesson(course_id=course.id, trilium_note_id="lesson-2", title="Lesson 2", parent_note_id="course", content_hash="hash")
        without_video = Lesson(course_id=course.id, trilium_note_id="lesson-3", title="Lesson 3", parent_note_id="course", content_hash="hash")
        session.add_all([with_video, also_with_video, without_video])
        session.flush()
        session.add(YouTubeUpload(lesson_id=with_video.id, video_id="a", video_url="https://www.youtube.com/watch?v=a"))
        session.add(YouTubeUpload(lesson_id=also_with_video.id, video_id="b", video_url="https://www.youtube.com/watch?v=b"))
        session.commit()
        course_id = course.id

    queued_urls: list[str] = []

    async def fake_enqueue(queue_url: str, youtube_url: str) -> None:
        queued_urls.append(youtube_url)

    monkeypatch.setattr("app.main.enqueue_audio_stream", fake_enqueue)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/courses/{course_id}/queue-audio-all", headers={"Accept": "application/json"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["level"] == "info"
    assert "Queued 2 lesson(s)" in payload["message"]
    assert sorted(queued_urls) == ["https://www.youtube.com/watch?v=a", "https://www.youtube.com/watch?v=b"]


def test_queue_audio_all_requires_at_least_one_youtube_upload(tmp_path: Path):
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
        course_id = course.id

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(f"/courses/{course_id}/queue-audio-all", headers={"Accept": "application/json"})

    assert response.status_code == 409
    payload = response.json()
    assert payload["level"] == "error"
    assert "No lessons in this course have a YouTube upload yet." in payload["message"]


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


def test_course_promoted_from_lesson_shows_legacy_video_and_flashcards(tmp_path: Path):
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        old_course = Course(
            trilium_note_id="old-course",
            title="Introduction to Philosophy",
            parent_note_id="catalog",
            traversal_hash="old-course",
            archived_at=datetime.now(timezone.utc),
        )
        session.add(old_course)
        session.flush()
        legacy_lesson = Lesson(
            course_id=old_course.id,
            trilium_note_id="note-shared",
            title="What is Philosophy?",
            parent_note_id="old-course",
            content_hash="",
            stage="publish",
            stage_state="completed",
            archived_at=datetime.now(timezone.utc),
        )
        session.add(legacy_lesson)
        session.flush()
        session.add(YouTubeUpload(lesson_id=legacy_lesson.id, video_id="abc123", video_url="https://www.youtube.com/watch?v=abc123"))
        session.add(Flashcard(lesson_id=legacy_lesson.id, prompt="Q1", answer="A1"))
        session.add(Flashcard(lesson_id=legacy_lesson.id, prompt="Q2", answer="A2", suspended=True))

        new_course = Course(trilium_note_id="note-shared", title="What is Philosophy?", parent_note_id="class-a", traversal_hash="note-shared")
        session.add(new_course)
        session.flush()
        session.add(Lesson(course_id=new_course.id, trilium_note_id="sub-note", title="Subtopic", parent_note_id="note-shared", content_hash=""))
        session.commit()
        new_course_id = new_course.id

    app = create_app(settings)
    with TestClient(app) as client:
        dashboard = client.get("/")
        course_page = client.get(f"/courses/{new_course_id}")

    assert dashboard.status_code == 200
    assert "Course video" in dashboard.text
    assert "1 flashcards" in dashboard.text
    assert course_page.status_code == 200
    assert "https://www.youtube.com/watch?v=abc123" in course_page.text
    assert "1 flashcard in the study deck" in course_page.text
    assert "Generated for the whole course before lessons were split out." in course_page.text


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


def test_new_card_daily_limit_gates_the_queue(tmp_path: Path):
    now = datetime.now(timezone.utc)
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
        session.add_all(
            [
                Flashcard(lesson_id=lesson.id, prompt="New A", answer="A", due_at=now),
                Flashcard(lesson_id=lesson.id, prompt="New B", answer="B", due_at=now),
            ]
        )
        session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        stats_response = client.get("/study")
        assert "1 / 1" not in stats_response.text  # sanity: default daily_new_cards is 10

        first = client.post("/study/1/review", data={"result": "pass"}, headers={"Accept": "application/json"})
        assert first.status_code == 200
        assert first.json()["stats"]["new_today"] == 1


def test_new_card_limit_of_one_withholds_second_new_card(tmp_path: Path):
    now = datetime.now(timezone.utc)
    # Learn-ahead off so the just-reviewed learning card is not served back.
    settings = make_settings(tmp_path, DAILY_NEW_CARDS="1", LEARN_AHEAD_MINUTES="0")
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)

    with session_factory() as session:
        course = Course(trilium_note_id="course", title="Course", parent_note_id=None, traversal_hash="course")
        session.add(course)
        session.flush()
        lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson 1", parent_note_id="course", content_hash="hash")
        session.add(lesson)
        session.flush()
        card_a = Flashcard(lesson_id=lesson.id, prompt="New A", answer="A", due_at=now)
        card_b = Flashcard(lesson_id=lesson.id, prompt="New B", answer="B", due_at=now)
        session.add_all([card_a, card_b])
        session.commit()
        card_a_id = card_a.id

    app = create_app(settings)
    with TestClient(app) as client:
        review = client.post(
            f"/study/{card_a_id}/review", data={"result": "pass"}, headers={"Accept": "application/json"}
        )
        assert review.status_code == 200
        payload = review.json()
        assert payload["stats"]["new_today"] == 1
        # Second new card should be withheld until tomorrow.
        assert payload["flashcard"] is None


def test_learn_ahead_serves_learning_step_after_failing_last_card(tmp_path: Path):
    now = datetime.now(timezone.utc)
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
        card = Flashcard(lesson_id=lesson.id, prompt="Only card", answer="A", due_at=now)
        session.add(card)
        session.commit()
        card_id = card.id

    app = create_app(settings)
    with TestClient(app) as client:
        # Failing the only card puts it in a learning step minutes away; with
        # nothing else to do the queue serves it early instead of going empty.
        first = client.post(f"/study/{card_id}/review", data={"result": "again"}, headers={"Accept": "application/json"})
        assert first.status_code == 200
        payload = first.json()
        assert payload["flashcard"] is not None
        assert payload["flashcard"]["id"] == card_id

        # Simulate real elapsed time (the anti-double-submit guard only blocks
        # a regrade within a few seconds of the last one).
        with session_factory() as session:
            card = session.get(Flashcard, card_id)
            card.last_reviewed_at = card.last_reviewed_at - timedelta(minutes=1)
            session.commit()

        # The learn-ahead card can actually be graded despite due_at being in the future.
        second = client.post(f"/study/{card_id}/review", data={"result": "pass"}, headers={"Accept": "application/json"})
        assert second.status_code == 200

    with session_factory() as session:
        assert session.query(FlashcardReview).filter_by(flashcard_id=card_id).count() == 2


def test_learn_ahead_does_not_allow_double_submitting_a_learning_card(tmp_path: Path):
    # Learning/relearning steps are minutes long -- shorter than the default
    # learn-ahead window -- so a naive "due_at within horizon" check alone
    # would keep accepting a rapid resubmission of the same card forever.
    now = datetime.now(timezone.utc)
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
        card = Flashcard(lesson_id=lesson.id, prompt="Only card", answer="A", due_at=now)
        session.add(card)
        session.commit()
        card_id = card.id

    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post(f"/study/{card_id}/review", data={"result": "again"}, headers={"Accept": "application/json"})
        assert first.status_code == 200

        # A double-click / retried POST milliseconds later must not grade again.
        duplicate = client.post(f"/study/{card_id}/review", data={"result": "pass"}, headers={"Accept": "application/json"})
        assert duplicate.status_code == 200

    with session_factory() as session:
        assert session.query(FlashcardReview).filter_by(flashcard_id=card_id).count() == 1


def test_suspend_edit_delete_and_undo_routes(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    ids = seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        suspend = client.post(
            f"/study/{ids['due_card_id']}/suspend", headers={"Accept": "application/json"}
        )
        assert suspend.status_code == 200
        with session_factory() as session:
            assert session.get(Flashcard, ids["due_card_id"]).suspended is True

        unsuspend = client.post(
            f"/study/{ids['due_card_id']}/unsuspend", headers={"Accept": "application/json"}
        )
        assert unsuspend.status_code == 200
        with session_factory() as session:
            assert session.get(Flashcard, ids["due_card_id"]).suspended is False

        edited = client.post(
            f"/study/{ids['due_card_id']}/edit",
            data={"prompt": "Updated prompt", "answer": "Updated answer"},
            headers={"Accept": "application/json"},
        )
        assert edited.status_code == 200
        with session_factory() as session:
            card = session.get(Flashcard, ids["due_card_id"])
            assert card.prompt == "Updated prompt"
            assert card.answer == "Updated answer"

        review = client.post(
            f"/study/{ids['due_card_id']}/review", data={"result": "pass"}, headers={"Accept": "application/json"}
        )
        assert review.status_code == 200
        with session_factory() as session:
            assert session.query(FlashcardReview).filter_by(flashcard_id=ids["due_card_id"]).count() == 2

        undo = client.post("/study/undo", headers={"Accept": "application/json"})
        assert undo.status_code == 200
        assert undo.json()["flashcard"]["id"] == ids["due_card_id"]
        with session_factory() as session:
            assert session.query(FlashcardReview).filter_by(flashcard_id=ids["due_card_id"]).count() == 1

        delete = client.post(
            f"/study/{ids['future_card_id']}/delete", headers={"Accept": "application/json"}
        )
        assert delete.status_code == 200
        with session_factory() as session:
            assert session.get(Flashcard, ids["future_card_id"]) is None


def test_study_page_renders_analytics_section(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path)
    session_factory, engine = make_session_factory(settings)
    Base.metadata.create_all(bind=engine)
    seed_study_data(session_factory, now=now)

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/study")

    assert response.status_code == 200
    assert "Day streak" in response.text
    assert "Retention (30d)" in response.text
    assert "Due in the next 7 days" in response.text
    assert "Last 13 weeks" in response.text


def test_reviewed_today_uses_los_angeles_day_boundary(tmp_path: Path):
    now = datetime.now(timezone.utc)
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
        card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=now)
        session.add(card)
        session.flush()
        # A review one minute before today's Los Angeles midnight belongs to the
        # previous LA day and must never count toward today's totals.
        la_midnight_utc = (
            now.astimezone(ZoneInfo("America/Los_Angeles"))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        review_time = la_midnight_utc - timedelta(minutes=1)
        session.add(
            FlashcardReview(
                flashcard_id=card.id,
                result="pass",
                scheduled_due_at=review_time,
                reviewed_at=review_time,
                next_due_at=review_time + timedelta(days=1),
                ease_factor_after=2.5,
                interval_days_after=1,
                repetitions_after=1,
                state_before="new",
            )
        )
        session.commit()

    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/study")

    assert page.status_code == 200
    assert '<span data-stat="reviewed_today">0</span>' in page.text


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
