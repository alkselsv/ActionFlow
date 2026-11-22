"""Add commitments, their event history and commitment rule snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0008_commitments"
down_revision = "0007_sla_priority"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "commitment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False
        ),
        sa.Column(
            "anchor_message_id",
            sa.String(36),
            sa.ForeignKey("message.id"),
            nullable=False,
        ),
        sa.Column(
            "anchor_revision_id",
            sa.String(36),
            sa.ForeignKey("message_revision.id"),
            nullable=False,
        ),
        sa.Column("subject_text", sa.Text(), nullable=False),
        sa.Column("assignee_name", sa.String(200), nullable=True),
        sa.Column(
            "assignee_participant_id",
            sa.String(36),
            sa.ForeignKey("participant.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_uncertain", sa.Boolean(), nullable=False),
        sa.Column("explicit_risk", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "anchor_message_id"),
    )
    op.create_index(
        "ix_commitment_project_status",
        "commitment",
        ["project_id", "status"],
    )

    op.create_table(
        "commitment_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "commitment_id",
            sa.String(36),
            sa.ForeignKey("commitment.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "message_id", sa.String(36), sa.ForeignKey("message.id"), nullable=True
        ),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("message_revision.id"),
            nullable=True,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "commitment_id", "kind", "message_id", "revision_id"
        ),
    )
    op.create_index(
        "ix_commitment_event_commitment_at",
        "commitment_event",
        ["commitment_id", "at"],
    )

    op.add_column(
        "finding",
        sa.Column("commitment_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "finding", sa.Column("commitment_status", sa.String(20), nullable=True)
    )
    op.create_index(
        "ix_finding_commitment",
        "finding",
        ["project_id", "finding_type", "commitment_status"],
    )

    with op.batch_alter_table("finding_observation") as batch:
        batch.add_column(
            sa.Column(
                "commitment_due_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch.add_column(
            sa.Column("commitment_status", sa.String(20), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("finding_observation") as batch:
        batch.drop_column("commitment_status")
        batch.drop_column("commitment_due_at")
    op.drop_index("ix_finding_commitment", table_name="finding")
    op.drop_column("finding", "commitment_status")
    op.drop_column("finding", "commitment_due_at")
    op.drop_index("ix_commitment_event_commitment_at", table_name="commitment_event")
    op.drop_table("commitment_event")
    op.drop_index("ix_commitment_project_status", table_name="commitment")
    op.drop_table("commitment")
