from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Course, Flashcard, FlashcardReview, Lesson

GRADE_TO_RATING = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "pass": Rating.Good,
    "easy": Rating.Easy,
}

STATE_LABELS = {
    State.Learning: "learning",
    State.Review: "review",
    State.Relearning: "relearning",
}

LABEL_STATES = {label: state for state, label in STATE_LABELS.items()}

# A course's study class falls back to its own title when no class is set;
# shared by the study queue scoping and undo so they can never disagree.
study_class_expr = func.coalesce(Course.class_title, Course.title)


@dataclass
class ReviewOutcome:
    result: str
    scheduled_due_at: datetime
    reviewed_at: datetime
    next_due_at: datetime
    ease_factor_after: float
    interval_days_after: int
    repetitions_after: int
    state_before: str
    card_before_json: str
    card_after_json: str


def build_scheduler(settings: Settings) -> Scheduler:
    return Scheduler(
        desired_retention=settings.desired_retention,
        enable_fuzzing=settings.fsrs_enable_fuzzing,
    )


def replay_scheduler() -> Scheduler:
    # Deterministic scheduler for migrations/replays; must not depend on env settings.
    return Scheduler(desired_retention=0.9, enable_fuzzing=False)


def utc_datetime(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def card_from_flashcard(flashcard: Flashcard) -> Card:
    if flashcard.stability is None:
        return Card(due=utc_datetime(flashcard.due_at))
    return Card(
        state=LABEL_STATES.get(flashcard.fsrs_state or "", State.Review),
        step=flashcard.fsrs_step,
        stability=flashcard.stability,
        difficulty=flashcard.difficulty,
        due=utc_datetime(flashcard.due_at),
        last_review=utc_datetime(flashcard.last_reviewed_at) if flashcard.last_reviewed_at else None,
    )


def card_state_label(flashcard: Flashcard, card: Card) -> str:
    return "new" if flashcard.stability is None else STATE_LABELS[card.state]


def interval_days_between(reviewed_at: datetime, due: datetime) -> int:
    return max(0, round((due - reviewed_at).total_seconds() / 86400))


def apply_review(
    scheduler: Scheduler,
    flashcard: Flashcard,
    result: str,
    reviewed_at: datetime | None = None,
) -> ReviewOutcome:
    reviewed_at = utc_datetime(reviewed_at or datetime.now(timezone.utc))
    scheduled_due = flashcard.due_at
    card = card_from_flashcard(flashcard)
    state_before = card_state_label(flashcard, card)
    card_before = card.to_dict()
    updated, _ = scheduler.review_card(card, GRADE_TO_RATING[result], review_datetime=reviewed_at)
    card_after = updated.to_dict()

    flashcard.stability = updated.stability
    flashcard.difficulty = updated.difficulty
    flashcard.fsrs_state = STATE_LABELS[updated.state]
    flashcard.fsrs_step = updated.step
    flashcard.due_at = updated.due
    flashcard.last_reviewed_at = reviewed_at
    flashcard.repetitions = flashcard.repetitions + 1
    flashcard.interval_days = interval_days_between(reviewed_at, updated.due)

    return ReviewOutcome(
        result=result,
        scheduled_due_at=scheduled_due,
        reviewed_at=reviewed_at,
        next_due_at=updated.due,
        ease_factor_after=flashcard.ease_factor,
        interval_days_after=flashcard.interval_days,
        repetitions_after=flashcard.repetitions,
        state_before=state_before,
        card_before_json=json.dumps(card_before),
        card_after_json=json.dumps(card_after),
    )


def create_flashcard_review(flashcard: Flashcard, outcome: ReviewOutcome) -> FlashcardReview:
    return FlashcardReview(
        flashcard=flashcard,
        result=outcome.result,
        scheduled_due_at=outcome.scheduled_due_at,
        reviewed_at=outcome.reviewed_at,
        next_due_at=outcome.next_due_at,
        ease_factor_after=outcome.ease_factor_after,
        interval_days_after=outcome.interval_days_after,
        repetitions_after=outcome.repetitions_after,
        state_before=outcome.state_before,
        card_before_json=outcome.card_before_json,
        card_after_json=outcome.card_after_json,
    )


def seed_fsrs_state(session: Session, scheduler: Scheduler | None = None) -> int:
    """Backfill FSRS card state by replaying each card's review history.

    Cards with no reviews (or a post-reset repetitions of 0) stay "new" with
    NULL FSRS columns. Existing due_at values are preserved so the upgrade does
    not re-dump the whole deck at once. Returns the number of cards seeded.
    """
    scheduler = scheduler or replay_scheduler()
    seeded = 0
    flashcards = session.scalars(select(Flashcard)).all()
    for flashcard in flashcards:
        if flashcard.stability is not None:
            continue
        reviews = session.scalars(
            select(FlashcardReview)
            .where(FlashcardReview.flashcard_id == flashcard.id)
            .order_by(FlashcardReview.reviewed_at.asc(), FlashcardReview.id.asc())
        ).all()
        if not reviews:
            continue
        # SM-2 left repetitions at 0 both after a deck reset (interval 0) and
        # after a lapse ("again" set interval to 1). Only a reset should leave
        # the card "new"; a lapsed card keeps its replayed schedule.
        was_reset = flashcard.repetitions == 0 and flashcard.interval_days == 0
        card = Card()
        for review in reviews:
            if review.state_before is None:
                review.state_before = "new" if card.stability is None else STATE_LABELS[card.state]
            rating = GRADE_TO_RATING.get(review.result, Rating.Good)
            card, _ = scheduler.review_card(card, rating, review_datetime=utc_datetime(review.reviewed_at))
        if was_reset:
            continue
        flashcard.stability = card.stability
        flashcard.difficulty = card.difficulty
        flashcard.fsrs_state = STATE_LABELS[card.state]
        flashcard.fsrs_step = card.step
        flashcard.last_reviewed_at = reviews[-1].reviewed_at
        seeded += 1
    return seeded


def undo_last_review(session: Session, class_title: str | None = None) -> tuple[Flashcard | None, str | None]:
    """Revert the most recent review; returns (restored flashcard, error message)."""
    stmt = (
        select(FlashcardReview)
        .join(Flashcard, Flashcard.id == FlashcardReview.flashcard_id)
        .order_by(FlashcardReview.reviewed_at.desc(), FlashcardReview.id.desc())
        .limit(1)
    )
    if class_title:
        stmt = (
            stmt.join(Lesson, Lesson.id == Flashcard.lesson_id)
            .join(Course, Course.id == Lesson.course_id)
            .where(study_class_expr == class_title)
        )
    review = session.scalar(stmt)
    if review is None:
        return None, "No review to undo."
    if review.card_before_json is None:
        return None, "The last review predates FSRS scheduling and cannot be undone."

    flashcard = review.flashcard
    if flashcard.stability is None:
        # The deck was reset (or reseeded) after this review; restoring the
        # snapshot would silently resurrect pre-reset scheduling.
        return None, "The last review predates a deck reset and cannot be undone."
    snapshot = Card.from_dict(json.loads(review.card_before_json))
    if snapshot.stability is None:
        flashcard.stability = None
        flashcard.difficulty = None
        flashcard.fsrs_state = None
        flashcard.fsrs_step = None
    else:
        flashcard.stability = snapshot.stability
        flashcard.difficulty = snapshot.difficulty
        flashcard.fsrs_state = STATE_LABELS[snapshot.state]
        flashcard.fsrs_step = snapshot.step
    flashcard.due_at = review.scheduled_due_at
    flashcard.last_reviewed_at = snapshot.last_review
    flashcard.repetitions = max(0, flashcard.repetitions - 1)
    if snapshot.last_review and snapshot.due:
        flashcard.interval_days = interval_days_between(snapshot.last_review, snapshot.due)
    else:
        flashcard.interval_days = 0
    session.delete(review)
    return flashcard, None
