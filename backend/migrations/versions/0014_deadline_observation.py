"""Retain timer checks separately from historical analysis."""

import sqlalchemy as sa
from alembic import op

revision = "0014_deadline_observation"
down_revision = "0013_input_receipt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("commitment", sa.Column("due_settings_version_id", sa.String(36)))
    op.create_table(
        "deadline_observation",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "finding_id", sa.String(36), sa.ForeignKey("finding.id"), nullable=False
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column(
            "settings_version_id", sa.String(36), sa.ForeignKey("settings_version.id")
        ),
    )
    op.create_index(
        "ix_deadline_observation_finding_at",
        "deadline_observation",
        ["finding_id", "evaluated_at"],
    )


def downgrade():
    op.drop_table("deadline_observation")
    op.drop_column("commitment", "due_settings_version_id")
