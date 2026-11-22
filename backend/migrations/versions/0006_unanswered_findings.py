"""Persist unanswered candidates, evidence and durable finding cards."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_unanswered_findings"
down_revision = "0005_analysis_input"
branch_labels = None
depends_on = None


def document():
    return sa.JSON().with_variant(JSONB(), "postgresql")


def timestamp(name, *, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    op.add_column(
        "analysis_run", sa.Column("detector_version", sa.String(80), nullable=True)
    )
    op.add_column(
        "analysis_run", sa.Column("prompt_version", sa.String(80), nullable=True)
    )
    op.add_column(
        "analysis_run", sa.Column("schema_version", sa.String(80), nullable=True)
    )
    op.add_column("analysis_run", sa.Column("provider", sa.String(80), nullable=True))
    op.add_column("analysis_run", sa.Column("model_id", sa.String(120), nullable=True))
    op.add_column("analysis_run", sa.Column("model_params", document(), nullable=True))
    op.add_column("analysis_run", sa.Column("usage", document(), nullable=True))
    op.add_column(
        "analysis_run", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True)
    )
    op.add_column(
        "analysis_run",
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analysis_run",
        sa.Column("finding_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "analysis_run",
        sa.Column(
            "invalid_candidate_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "analysis_run",
        sa.Column("detector_duration_ms", sa.Integer(), nullable=True),
    )

    op.create_table(
        "candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False
        ),
        sa.Column("finding_type", sa.String(20), nullable=False),
        sa.Column("candidate_key", sa.String(128), nullable=False),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
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
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("priority_hint", sa.String(20), nullable=False),
        sa.Column("suggested_action", sa.String(30), nullable=False),
        sa.Column("limitations", document(), nullable=False),
        sa.Column("validation_status", sa.String(20), nullable=False),
        sa.Column("validation_errors", document(), nullable=False),
        sa.Column("raw_payload", document(), nullable=False),
        timestamp("created_at"),
        sa.UniqueConstraint("run_id", "candidate_key"),
    )
    op.create_index("ix_candidate_run", "candidate", ["run_id"])
    op.create_index(
        "ix_candidate_project_type", "candidate", ["project_id", "finding_type"]
    )

    op.create_table(
        "candidate_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.String(36),
            sa.ForeignKey("candidate.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("message.id"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            sa.String(36),
            sa.ForeignKey("message_revision.id"),
            nullable=False,
        ),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("quote_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint(
            "candidate_id", "role", "message_id", "revision_id", "quote_hash"
        ),
    )
    op.create_index(
        "ix_candidate_evidence_candidate", "candidate_evidence", ["candidate_id"]
    )

    op.create_table(
        "finding",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=False
        ),
        sa.Column("finding_type", sa.String(20), nullable=False),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
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
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("priority_reason", document(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("suggested_action", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_state", sa.String(30), nullable=False),
        sa.Column(
            "latest_candidate_id",
            sa.String(36),
            sa.ForeignKey("candidate.id"),
            nullable=True,
        ),
        sa.Column(
            "current_run_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.id"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.UniqueConstraint(
            "project_id", "finding_type", "subject_kind", "subject_id"
        ),
    )
    op.create_index(
        "ix_finding_queue",
        "finding",
        ["project_id", "status", "priority", "updated_at"],
    )

    op.create_table(
        "finding_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "finding_id", sa.String(36), sa.ForeignKey("finding.id"), nullable=False
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("analysis_run.id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(36),
            sa.ForeignKey("candidate.id"),
            nullable=False,
        ),
        timestamp("observed_at"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.UniqueConstraint("finding_id", "run_id"),
    )
    op.create_index(
        "ix_finding_observation_finding_observed",
        "finding_observation",
        ["finding_id", "observed_at"],
    )


def downgrade():
    op.drop_index(
        "ix_finding_observation_finding_observed", table_name="finding_observation"
    )
    op.drop_table("finding_observation")
    op.drop_index("ix_finding_queue", table_name="finding")
    op.drop_table("finding")
    op.drop_index(
        "ix_candidate_evidence_candidate", table_name="candidate_evidence"
    )
    op.drop_table("candidate_evidence")
    op.drop_index("ix_candidate_project_type", table_name="candidate")
    op.drop_index("ix_candidate_run", table_name="candidate")
    op.drop_table("candidate")
    for column in (
        "detector_duration_ms",
        "invalid_candidate_count",
        "finding_count",
        "candidate_count",
        "cost_usd",
        "usage",
        "model_params",
        "model_id",
        "provider",
        "schema_version",
        "prompt_version",
        "detector_version",
    ):
        op.drop_column("analysis_run", column)
