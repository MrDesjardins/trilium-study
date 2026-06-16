from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_archive_courses_lessons"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    course_columns = {column["name"] for column in inspector.get_columns("courses")}
    lesson_columns = {column["name"] for column in inspector.get_columns("lessons")}
    if "archived_at" not in course_columns:
        with op.batch_alter_table("courses") as batch:
            batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    if "archived_at" not in lesson_columns:
        with op.batch_alter_table("lessons") as batch:
            batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    course_columns = {column["name"] for column in inspector.get_columns("courses")}
    lesson_columns = {column["name"] for column in inspector.get_columns("lessons")}
    if "archived_at" in lesson_columns:
        with op.batch_alter_table("lessons") as batch:
            batch.drop_column("archived_at")
    if "archived_at" in course_columns:
        with op.batch_alter_table("courses") as batch:
            batch.drop_column("archived_at")
