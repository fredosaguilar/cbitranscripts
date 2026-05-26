"""add schedule interval to webhook state

Revision ID: c4a7e9d8b123
Revises: 0c522e300604
Create Date: 2026-04-14 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4a7e9d8b123"
down_revision: Union[str, Sequence[str], None] = "0c522e300604"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("webhook_state")}
    if "schedule_interval_seconds" not in columns:
        op.add_column(
            "webhook_state",
            sa.Column("schedule_interval_seconds", sa.Integer(), nullable=True),
        )

    op.execute(
        """
        UPDATE webhook_state
        SET schedule_interval_seconds = COALESCE(schedule_interval_seconds, 60)
        """
    )

    op.alter_column("webhook_state", "schedule_interval_seconds", nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("webhook_state")}
    if "schedule_interval_seconds" in columns:
        op.drop_column("webhook_state", "schedule_interval_seconds")
