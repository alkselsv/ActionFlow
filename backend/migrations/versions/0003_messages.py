"""Store imported Telegram messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_messages"
down_revision = "0002_project_settings"
branch_labels = None
depends_on = None


def upgrade():
    document = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "message",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chat.id"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("participant_id", sa.String(36), sa.ForeignKey("participant.id")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.String, nullable=False),
        sa.Column("text_format", document, nullable=False),
        sa.Column("reply_to_external_id", sa.String(200)),
        sa.UniqueConstraint("chat_id", "external_id"),
    )
    op.create_index("ix_message_chat_sent", "message", ["chat_id", "sent_at", "external_id"])


def downgrade():
    op.drop_table("message")
