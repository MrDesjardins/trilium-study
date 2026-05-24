from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_session_factory
from app.main import create_app
from app.models import Course, Flashcard, FlashcardReview, Lesson


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
                    reviewed_at=now - timedelta(hours=1),
                    next_due_at=now + timedelta(days=1),
                    ease_factor_after=2.6,
                    interval_days_after=1,
                    repetitions_after=1,
                ),
                FlashcardReview(
                    flashcard_id=failed_due_card.id,
                    result="again",
                    scheduled_due_at=now - timedelta(hours=3),
                    reviewed_at=now - timedelta(minutes=30),
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
