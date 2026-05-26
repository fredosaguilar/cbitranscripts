"""create webhook state table

Revision ID: b91d4a2c6f10
Revises: e6f8a4b9c2d1
Create Date: 2026-04-07 18:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b91d4a2c6f10"
down_revision: Union[str, Sequence[str], None] = "e6f8a4b9c2d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("webhook_state"):
        return

    op.create_table(
        "webhook_state",
        sa.Column("state_key", sa.String(), nullable=False),
        sa.Column("start_time", sa.TIMESTAMP(), nullable=True),
        sa.Column("start_time_raw", sa.String(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("state_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("webhook_state")
