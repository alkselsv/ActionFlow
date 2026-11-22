"""Add explainable SLA and priority results to findings and observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_sla_priority"
down_revision = "0006_unanswered_findings"
branch_labels = None
depends_on = None


def document():
    return sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade():
    op.add_column(
        "finding",
        sa.Column("sla_reason", document(), nullable=True),
    )
    op.add_column(
        "finding",
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_finding_sla", "finding", ["project_id", "sla_state", "priority"]
    )

    with op.batch_alter_table("finding_observation") as batch:
        batch.add_column(sa.Column("priority", sa.String(20), nullable=True))
        batch.add_column(sa.Column("priority_reason", document(), nullable=True))
        batch.add_column(
            sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("sla_state", sa.String(30), nullable=True))
        batch.add_column(sa.Column("sla_reason", document(), nullable=True))
        batch.add_column(
            sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "settings_version_id",
                sa.String(36),
                sa.ForeignKey(
                    "settings_version.id",
                    name="fk_finding_observation_settings_version",
                ),
                nullable=True,
            )
        )


def downgrade():
    with op.batch_alter_table("finding_observation") as batch:
        batch.drop_column("settings_version_id")
        batch.drop_column("first_response_at")
        batch.drop_column("sla_reason")
        batch.drop_column("sla_state")
        batch.drop_column("sla_due_at")
        batch.drop_column("priority_reason")
        batch.drop_column("priority")
    op.drop_index("ix_finding_sla", table_name="finding")
    op.drop_column("finding", "first_response_at")
    op.drop_column("finding", "sla_reason")
