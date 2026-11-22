"""Add worker heartbeat metadata for operational diagnostics."""

import sqlalchemy as sa
from alembic import op

revision = "0010_operations"
down_revision = "0009_manager_actions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeat",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("task_queue", sa.String(120), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_worker_heartbeat_last_seen_at",
        "worker_heartbeat",
        ["last_seen_at"],
    )


def downgrade():
    op.drop_index(
        "ix_worker_heartbeat_last_seen_at", table_name="worker_heartbeat"
    )
    op.drop_table("worker_heartbeat")
