"""Add versioned project settings and participant identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_project_settings"
down_revision = "0001_manager_projects"
branch_labels = None
depends_on = None


def upgrade():
    document = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "chat",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("project.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_table(
        "participant",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chat.id"), nullable=False),
        sa.Column("source_author_id", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.UniqueConstraint("chat_id", "source_author_id"),
    )
    op.create_table(
        "settings_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("context_text", sa.String, nullable=False),
        sa.Column("agreements", sa.String, nullable=False),
        sa.Column("manager_name", sa.String(200), nullable=False),
        sa.Column("client_type", sa.String(20), nullable=False),
        sa.Column("work_hours", document, nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("first_response_minutes", sa.Integer, nullable=False),
        sa.Column("calendar", document, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "version_no"),
    )
    op.create_table(
        "participant_side",
        sa.Column(
            "settings_version_id",
            sa.String(36),
            sa.ForeignKey("settings_version.id"),
            primary_key=True,
        ),
        sa.Column("source_author_id", sa.String(200), primary_key=True),
        sa.Column("side", sa.String(10), nullable=False),
    )


def downgrade():
    for table in ("participant_side", "settings_version", "participant", "chat"):
        op.drop_table(table)
