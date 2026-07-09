from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Course, Flashcard, FlashcardReview, Lesson
from app.srs import (
    apply_review,
    build_scheduler,
    create_flashcard_review,
    replay_scheduler,
    seed_fsrs_state,
    undo_last_review,
)


def make_settings(**overrides) -> Settings:
    data = {
        "TRILIUM_URL": "http://example",
        "TRILIUM_ETAPI_TOKEN": "token",
        "TRILIUM_PARENT_NOTE_ID": "course",
        "FSRS_ENABLE_FUZZING": False,
    }
    data.update(overrides)
    return Settings.model_validate(data)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def make_lesson(session: Session) -> Lesson:
    course = Course(trilium_note_id="course-1", title="Course", traversal_hash="h")
    session.add(course)
    session.flush()
    lesson = Lesson(course_id=course.id, trilium_note_id="lesson-1", title="Lesson", content_hash="h")
    session.add(lesson)
    session.flush()
    return lesson


def test_new_card_first_review_enters_learning_with_subday_due():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()

    scheduler = build_scheduler(make_settings())
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    outcome = apply_review(scheduler, card, "pass", now)

    assert outcome.state_before == "new"
    assert card.fsrs_state in {"learning", "review"}
    assert card.due_at > now
    assert card.due_at < now + timedelta(days=1)
    assert card.stability is not None


def test_again_on_review_card_enters_relearning():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()

    scheduler = build_scheduler(make_settings())
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    apply_review(scheduler, card, "pass", now)
    apply_review(scheduler, card, "pass", now + timedelta(days=5))
    outcome = apply_review(scheduler, card, "again", now + timedelta(days=20))

    assert outcome.state_before == "review"
    assert card.fsrs_state == "relearning"


def test_fuzz_off_scheduling_is_deterministic():
    settings = make_settings()
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)

    session_a = make_session()
    card_a = Flashcard(lesson_id=make_lesson(session_a).id, prompt="Q", answer="A", due_at=now)
    session_a.add(card_a)
    session_a.flush()
    apply_review(build_scheduler(settings), card_a, "pass", now)

    session_b = make_session()
    card_b = Flashcard(lesson_id=make_lesson(session_b).id, prompt="Q", answer="A", due_at=now)
    session_b.add(card_b)
    session_b.flush()
    apply_review(build_scheduler(settings), card_b, "pass", now)

    assert card_a.due_at == card_b.due_at
    assert card_a.stability == card_b.stability


def test_apply_review_populates_snapshots():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()

    scheduler = build_scheduler(make_settings())
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    outcome = apply_review(scheduler, card, "pass", now)
    review = create_flashcard_review(card, outcome)
    session.add(review)
    session.commit()

    assert review.state_before == "new"
    before = json.loads(review.card_before_json)
    after = json.loads(review.card_after_json)
    assert before["stability"] is None
    assert after["stability"] is not None


def test_seed_fsrs_state_replays_history_and_preserves_due_at():
    session = make_session()
    lesson = make_lesson(session)
    preserved_due = datetime(2026, 8, 1, tzinfo=timezone.utc)
    card = Flashcard(
        lesson_id=lesson.id,
        prompt="Q",
        answer="A",
        due_at=preserved_due,
        repetitions=2,
        interval_days=5,
    )
    zero_rep_card = Flashcard(lesson_id=lesson.id, prompt="Q2", answer="A2", due_at=datetime.now(timezone.utc))
    session.add_all([card, zero_rep_card])
    session.flush()

    r1_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    r2_time = datetime(2026, 7, 3, tzinfo=timezone.utc)
    session.add_all(
        [
            FlashcardReview(
                flashcard_id=card.id,
                result="pass",
                scheduled_due_at=r1_time,
                reviewed_at=r1_time,
                next_due_at=r1_time + timedelta(days=1),
                ease_factor_after=2.5,
                interval_days_after=1,
                repetitions_after=1,
            ),
            FlashcardReview(
                flashcard_id=card.id,
                result="pass",
                scheduled_due_at=r2_time,
                reviewed_at=r2_time,
                next_due_at=r2_time + timedelta(days=3),
                ease_factor_after=2.5,
                interval_days_after=3,
                repetitions_after=2,
            ),
        ]
    )
    session.commit()

    seeded = seed_fsrs_state(session, replay_scheduler())
    session.commit()

    assert seeded == 1
    assert card.stability is not None
    due_at = card.due_at if card.due_at.tzinfo else card.due_at.replace(tzinfo=timezone.utc)
    assert due_at == preserved_due
    assert zero_rep_card.stability is None

    reviews = session.query(FlashcardReview).filter_by(flashcard_id=card.id).order_by(FlashcardReview.reviewed_at).all()
    assert reviews[0].state_before == "new"
    assert reviews[1].state_before in {"learning", "review"}


def test_seed_fsrs_state_replays_lapsed_cards_but_not_reset_cards():
    session = make_session()
    lesson = make_lesson(session)
    # Old SM-2 "again" left repetitions=0 with interval_days=1 (lapsed).
    lapsed = Flashcard(
        lesson_id=lesson.id, prompt="Lapsed", answer="A",
        due_at=datetime(2026, 7, 2, tzinfo=timezone.utc), repetitions=0, interval_days=1,
    )
    # A pre-upgrade deck reset left repetitions=0 with interval_days=0.
    reset = Flashcard(
        lesson_id=lesson.id, prompt="Reset", answer="A",
        due_at=datetime(2026, 7, 1, tzinfo=timezone.utc), repetitions=0, interval_days=0,
    )
    session.add_all([lapsed, reset])
    session.flush()
    for card in (lapsed, reset):
        r_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
        session.add_all(
            [
                FlashcardReview(
                    flashcard_id=card.id, result="pass", scheduled_due_at=r_time, reviewed_at=r_time,
                    next_due_at=r_time + timedelta(days=1), ease_factor_after=2.5,
                    interval_days_after=1, repetitions_after=1,
                ),
                FlashcardReview(
                    flashcard_id=card.id, result="again", scheduled_due_at=r_time + timedelta(days=1),
                    reviewed_at=r_time + timedelta(days=1), next_due_at=r_time + timedelta(days=2),
                    ease_factor_after=2.3, interval_days_after=1, repetitions_after=0,
                ),
            ]
        )
    session.commit()

    seeded = seed_fsrs_state(session, replay_scheduler())
    session.commit()

    assert seeded == 1
    assert lapsed.stability is not None
    assert lapsed.fsrs_state in {"learning", "relearning"}
    assert reset.stability is None
    # Even skipped (reset) cards get their history backfilled for analytics.
    for review in session.query(FlashcardReview).filter_by(flashcard_id=reset.id):
        assert review.state_before is not None


def test_undo_refuses_after_deck_reset():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()

    scheduler = build_scheduler(make_settings())
    outcome = apply_review(scheduler, card, "pass", datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc))
    session.add(create_flashcard_review(card, outcome))
    session.commit()

    # Simulate a deck reset after the review.
    card.stability = None
    card.difficulty = None
    card.fsrs_state = None
    card.fsrs_step = None
    card.last_reviewed_at = None
    session.commit()

    restored, error = undo_last_review(session)

    assert restored is None
    assert "reset" in error
    assert session.query(FlashcardReview).count() == 1


def test_undo_restores_prior_state_and_deletes_review():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()

    scheduler = build_scheduler(make_settings())
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    outcome = apply_review(scheduler, card, "pass", now)
    review = create_flashcard_review(card, outcome)
    session.add(review)
    session.commit()
    assert card.stability is not None

    restored, error = undo_last_review(session)
    session.commit()

    assert error is None
    assert restored.id == card.id
    assert card.stability is None
    assert card.repetitions == 0
    assert session.query(FlashcardReview).count() == 0


def test_undo_refuses_when_snapshot_missing():
    session = make_session()
    lesson = make_lesson(session)
    card = Flashcard(lesson_id=lesson.id, prompt="Q", answer="A", due_at=datetime.now(timezone.utc))
    session.add(card)
    session.flush()
    session.add(
        FlashcardReview(
            flashcard_id=card.id,
            result="pass",
            scheduled_due_at=datetime.now(timezone.utc),
            reviewed_at=datetime.now(timezone.utc),
            next_due_at=datetime.now(timezone.utc),
            ease_factor_after=2.5,
            interval_days_after=1,
            repetitions_after=1,
        )
    )
    session.commit()

    restored, error = undo_last_review(session)

    assert restored is None
    assert error is not None
