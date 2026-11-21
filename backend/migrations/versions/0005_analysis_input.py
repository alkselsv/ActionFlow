"""Persist analysis requests, immutable input references and delivery outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_analysis_input"
down_revision = "0004_message_revisions"
branch_labels = None
depends_on = None


def timestamp(name, *, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    observations = op.create_table(
        "source_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("message_revision.id"),
            nullable=False,
        ),
        timestamp("recorded_at"),
        timestamp("source_observed_at"),
    )
    op.create_index(
        "ix_source_observation_revision_recorded",
        "source_observation",
        ["revision_id", "recorded_at"],
    )
    revisions = sa.table(
        "message_revision",
        sa.column("id", sa.String),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("source_observed_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        observations.insert().from_select(
            ["id", "revision_id", "recorded_at", "source_observed_at"],
            sa.select(
                revisions.c.id,
                revisions.c.id,
                revisions.c.observed_at,
                revisions.c.source_observed_at,
            ).where(revisions.c.source_observed_at.is_not(None)),
        )
    )
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False
        ),
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chat.id")),
        sa.Column(
            "settings_version_id", sa.String(36), sa.ForeignKey("settings_version.id")
        ),
        timestamp("period_start"),
        timestamp("period_end"),
        timestamp("as_of"),
        sa.Column("apply_to_queue", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(90), nullable=False, unique=True),
        sa.Column("temporal_run_id", sa.String(36)),
        sa.Column("input_hash", sa.String(64)),
        sa.Column("limitations", sa.JSON().with_variant(JSONB(), "postgresql")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("retryable", sa.Boolean()),
        timestamp("created_at"),
        timestamp("started_at", nullable=True),
        timestamp("finished_at", nullable=True),
        sa.UniqueConstraint("project_id", "idempotency_key"),
    )
    op.create_index(
        "ix_analysis_run_project_created", "analysis_run", ["project_id", "created_at"]
    )
    op.create_table(
        "analysis_input_message",
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("analysis_run.id"), primary_key=True
        ),
        sa.Column(
            "message_id", sa.String(36), sa.ForeignKey("message.id"), primary_key=True
        ),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("message_revision.id"),
            nullable=False,
        ),
        timestamp("sent_at"),
        timestamp("observed_at"),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("participant_side", sa.String(10), nullable=False),
        timestamp("source_observed_at", nullable=True),
    )
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "aggregate_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.id"),
            nullable=False,
        ),
        sa.Column("event_key", sa.String(36), unique=True, nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        timestamp("next_attempt_at"),
        timestamp("dispatched_at", nullable=True),
        sa.Column("last_error", sa.String(64)),
    )
    op.create_index("ix_outbox_due", "outbox_event", ["status", "next_attempt_at"])


def downgrade():
    for table in (
        "outbox_event",
        "analysis_input_message",
        "analysis_run",
        "source_observation",
    ):
        op.drop_table(table)
