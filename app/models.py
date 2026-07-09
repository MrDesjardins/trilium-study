from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trilium_note_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    class_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parent_note_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    traversal_hash: Mapped[str] = mapped_column(String(64))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lessons: Mapped[list["Lesson"]] = relationship(back_populates="course", cascade="all, delete-orphan")


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (UniqueConstraint("course_id", "trilium_note_id", name="uq_lessons_course_note"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    trilium_note_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(512))
    parent_note_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    stage_state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    stage_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_content_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    flashcard_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    course: Mapped["Course"] = relationship(back_populates="lessons")
    artifacts: Mapped[list["LessonArtifact"]] = relationship(back_populates="lesson", cascade="all, delete-orphan")
    youtube_upload: Mapped["YouTubeUpload | None"] = relationship(back_populates="lesson", uselist=False, cascade="all, delete-orphan")
    flashcards: Mapped[list["Flashcard"]] = relationship(back_populates="lesson", cascade="all, delete-orphan")


class LessonArtifact(Base):
    __tablename__ = "lesson_artifacts"
    __table_args__ = (UniqueConstraint("lesson_id", "artifact_type", name="uq_lesson_artifact_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    content_hash: Mapped[str] = mapped_column(String(64))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lesson: Mapped["Lesson"] = relationship(back_populates="artifacts")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    force_regenerate: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    events: Mapped[list["JobEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped["Job"] = relationship(back_populates="events")


class YouTubeUpload(Base):
    __tablename__ = "youtube_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"), unique=True, index=True)
    video_id: Mapped[str] = mapped_column(String(128))
    video_url: Mapped[str] = mapped_column(Text)
    privacy_status: Mapped[str] = mapped_column(String(32), default="unlisted")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    lesson: Mapped["Lesson"] = relationship(back_populates="youtube_upload")


class Flashcard(Base):
    __tablename__ = "flashcards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ease_factor: Mapped[float] = mapped_column(Float, default=2.5)
    interval_days: Mapped[int] = mapped_column(Integer, default=0)
    repetitions: Mapped[int] = mapped_column(Integer, default=0)
    # FSRS state; stability is NULL until the first review ("new" card).
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    fsrs_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fsrs_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lesson: Mapped["Lesson"] = relationship(back_populates="flashcards")
    reviews: Mapped[list["FlashcardReview"]] = relationship(back_populates="flashcard", cascade="all, delete-orphan")


class FlashcardReview(Base):
    __tablename__ = "flashcard_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flashcard_id: Mapped[int] = mapped_column(ForeignKey("flashcards.id"), index=True)
    result: Mapped[str] = mapped_column(String(16))
    scheduled_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ease_factor_after: Mapped[float] = mapped_column(Float)
    interval_days_after: Mapped[int] = mapped_column(Integer)
    repetitions_after: Mapped[int] = mapped_column(Integer)
    # FSRS card state around this review; "new" means first-ever review of the card.
    state_before: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    card_before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_after_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    flashcard: Mapped["Flashcard"] = relationship(back_populates="reviews")
