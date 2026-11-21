"""Preserve message revisions and import retry receipts."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_message_revisions"
down_revision = "0003_messages"
branch_labels = None
depends_on = None


def upgrade():
    document = sa.JSON().with_variant(JSONB(), "postgresql")
    revisions = op.create_table(
        "message_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id", sa.String(36), sa.ForeignKey("message.id"), nullable=False
        ),
        sa.Column("revision_no", sa.Integer, nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("source_observed_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", document, nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False),
        sa.Column("ordering_uncertain", sa.Boolean, nullable=False),
        sa.UniqueConstraint("message_id", "revision_no"),
    )
    op.create_table(
        "import_receipt",
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("project.id"), primary_key=True
        ),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("report", document, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    messages = sa.table(
        "message",
        sa.column("id", sa.String),
        sa.column("text", sa.String),
        sa.column("text_format", document),
        sa.column("reply_to_external_id", sa.String),
        sa.column("participant_id", sa.String),
        sa.column("observed_at", sa.DateTime(timezone=True)),
    )
    rows = connection.execute(sa.select(messages)).mappings()
    for batch in rows.partitions(500):
        connection.execute(
            revisions.insert(),
            [
                {
                    "id": str(uuid4()),
                    "message_id": row["id"],
                    "revision_no": 1,
                    "observed_at": row["observed_at"],
                    "snapshot": {
                        key: row[key]
                        for key in (
                            "text",
                            "text_format",
                            "reply_to_external_id",
                            "participant_id",
                        )
                    },
                    "is_current": True,
                    "ordering_uncertain": False,
                }
                for row in batch
            ],
        )


def downgrade():
    op.drop_table("import_receipt")
    op.drop_table("message_revision")
