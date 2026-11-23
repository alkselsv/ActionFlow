"""Track the receipt cutoff for reproducible coalesced analysis input."""

import sqlalchemy as sa
from alembic import op

revision = "0013_input_receipt"
down_revision = "0012_telegram_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "analysis_run",
        sa.Column("input_received_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("analysis_run", "input_received_at")
