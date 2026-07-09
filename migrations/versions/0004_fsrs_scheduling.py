from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session


revision = "0004_fsrs_scheduling"
down_revision = "0003_course_class_title"
branch_labels = None
depends_on = None

FLASHCARD_COLUMNS = (
    sa.Column("stability", sa.Float(), nullable=True),
    sa.Column("difficulty", sa.Float(), nullable=True),
    sa.Column("fsrs_state", sa.String(16), nullable=True),
    sa.Column("fsrs_step", sa.Integer(), nullable=True),
    sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
)

REVIEW_COLUMNS = (
    sa.Column("state_before", sa.String(16), nullable=True),
    sa.Column("card_before_json", sa.Text(), nullable=True),
    sa.Column("card_after_json", sa.Text(), nullable=True),
)


def _missing_columns(inspector: sa.Inspector, table: str, columns: tuple[sa.Column, ...]) -> list[sa.Column]:
    existing = {column["name"] for column in inspector.get_columns(table)}
    return [column for column in columns if column.name not in existing]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    missing_flashcard = _missing_columns(inspector, "flashcards", FLASHCARD_COLUMNS)
    if missing_flashcard:
        with op.batch_alter_table("flashcards") as batch:
            for column in missing_flashcard:
                batch.add_column(column)

    missing_review = _missing_columns(inspector, "flashcard_reviews", REVIEW_COLUMNS)
    if missing_review:
        with op.batch_alter_table("flashcard_reviews") as batch:
            for column in missing_review:
                batch.add_column(column)

    if missing_flashcard:
        # Pre-existing database: seed FSRS state by replaying review history.
        from app.srs import seed_fsrs_state

        session = Session(bind=bind)
        seed_fsrs_state(session)
        session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_flashcard = {column["name"] for column in inspector.get_columns("flashcards")}
    to_drop = [column.name for column in FLASHCARD_COLUMNS if column.name in existing_flashcard]
    if to_drop:
        with op.batch_alter_table("flashcards") as batch:
            for name in to_drop:
                batch.drop_column(name)

    existing_review = {column["name"] for column in inspector.get_columns("flashcard_reviews")}
    to_drop = [column.name for column in REVIEW_COLUMNS if column.name in existing_review]
    if to_drop:
        with op.batch_alter_table("flashcard_reviews") as batch:
            for name in to_drop:
                batch.drop_column(name)
