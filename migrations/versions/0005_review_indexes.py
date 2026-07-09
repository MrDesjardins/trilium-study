from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_review_indexes"
down_revision = "0004_fsrs_scheduling"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_flashcard_reviews_reviewed_at", ["reviewed_at"]),
    ("ix_flashcard_reviews_state_before", ["state_before"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("flashcard_reviews")}
    for name, columns in INDEXES:
        if name not in existing:
            op.create_index(name, "flashcard_reviews", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("flashcard_reviews")}
    for name, _ in INDEXES:
        if name in existing:
            op.drop_index(name, table_name="flashcard_reviews")
