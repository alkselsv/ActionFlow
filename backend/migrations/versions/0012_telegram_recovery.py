"""Record Telegram recovery state and known gaps."""

import sqlalchemy as sa
from alembic import op

revision = "0012_telegram_recovery"
down_revision = "0011_telegram_connector"
branch_labels = None
depends_on = None


def upgrade():
    for table, fields in (
        (
            "telegram_connection",
            ("last_observed_at", "last_membership_at", "last_reconciled_at"),
        ),
        ("telegram_bot_state", ("last_update_at", "retry_at", "last_caught_up_at")),
    ):
        for field in fields:
            op.add_column(table, sa.Column(field, sa.DateTime(timezone=True)))
    op.add_column(
        "telegram_connection", sa.Column("last_membership_update_id", sa.BigInteger())
    )
    op.execute("UPDATE telegram_bot_state SET last_caught_up_at = last_polled_at")
    op.add_column(
        "telegram_bot_state",
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "telegram_update_receipt",
        sa.Column("bot_id", sa.String(200), primary_key=True),
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
    )
    op.create_table(
        "telegram_gap",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "chat_id",
            sa.String(36),
            sa.ForeignKey("telegram_connection.chat_id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_telegram_gap_chat", "telegram_gap", ["chat_id", "start_at"])


def downgrade():
    op.drop_table("telegram_gap")
    op.drop_table("telegram_update_receipt")
    for field in ("failures", "retry_at", "last_update_at", "last_caught_up_at"):
        op.drop_column("telegram_bot_state", field)
    for field in (
        "last_reconciled_at",
        "last_membership_at",
        "last_observed_at",
        "last_membership_update_id",
    ):
        op.drop_column("telegram_connection", field)
