"""Add manager actions, proposals and snooze time."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_manager_actions"
down_revision = "0008_commitments"
branch_labels = None
depends_on = None


def document():
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade():
    op.add_column(
        "finding",
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "finding_proposal",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "finding_id",
            sa.String(36),
            sa.ForeignKey("finding.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("analysis_run.id"), nullable=True
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", document(), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("finding_id", "kind", "source_key"),
    )
    op.create_index(
        "ix_finding_proposal_finding_status",
        "finding_proposal",
        ["finding_id", "status"],
    )

    op.create_table(
        "manager_action",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "finding_id",
            sa.String(36),
            sa.ForeignKey("finding.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "proposal_id",
            sa.String(36),
            sa.ForeignKey("finding_proposal.id"),
            nullable=True,
        ),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("app_user.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_body", document(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_manager_action_finding_created",
        "manager_action",
        ["finding_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_manager_action_finding_created", table_name="manager_action")
    op.drop_table("manager_action")
    op.drop_index(
        "ix_finding_proposal_finding_status", table_name="finding_proposal"
    )
    op.drop_table("finding_proposal")
    op.drop_column("finding", "snooze_until")
