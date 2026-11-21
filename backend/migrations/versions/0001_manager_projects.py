"""Create manager, session, client and project tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001_manager_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("login", sa.String(120), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="single_manager"),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_expires_at", "session", ["expires_at"])
    op.create_table(
        "client",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
    )
    op.create_table(
        "project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36), sa.ForeignKey("client.id"), nullable=False
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("manager_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "name"),
    )


def downgrade():
    op.drop_table("project")
    op.drop_table("client")
    op.drop_index("ix_session_expires_at", table_name="session")
    op.drop_table("session")
    op.drop_table("app_user")
