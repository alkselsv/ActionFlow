"""Add manual Telegram chat bindings and the Bot API update cursor."""

import sqlalchemy as sa
from alembic import op

revision = "0011_telegram_connector"
down_revision = "0010_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_connection",
        sa.Column(
            "chat_id",
            sa.String(36),
            sa.ForeignKey("chat.id"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(100)),
    )
    op.create_table(
        "telegram_bot_state",
        sa.Column("bot_id", sa.String(200), primary_key=True),
        sa.Column("username", sa.String(200)),
        sa.Column("last_update_id", sa.BigInteger()),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(100)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("telegram_bot_state")
    op.drop_table("telegram_connection")
