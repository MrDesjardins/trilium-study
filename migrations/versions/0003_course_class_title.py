from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_course_class_title"
down_revision = "0002_archive_courses_lessons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    course_columns = {column["name"] for column in inspector.get_columns("courses")}
    if "class_title" not in course_columns:
        with op.batch_alter_table("courses") as batch:
            batch.add_column(sa.Column("class_title", sa.String(512), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    course_columns = {column["name"] for column in inspector.get_columns("courses")}
    if "class_title" in course_columns:
        with op.batch_alter_table("courses") as batch:
            batch.drop_column("class_title")
