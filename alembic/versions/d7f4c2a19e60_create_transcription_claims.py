"""create transcription claims

Revision ID: d7f4c2a19e60
Revises: a1b2c3d4e5f6, e6670002ea38
Create Date: 2026-09-21 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f4c2a19e60"
down_revision: Union[str, Sequence[str], None] = ("a1b2c3d4e5f6", "e6670002ea38")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("transcription_claims"):
        return

    op.create_table(
        "transcription_claims",
        sa.Column("recording_id", sa.String(), nullable=False),
        sa.Column("claim_token", sa.String(), nullable=False),
        sa.Column("claimed_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("recording_id"),
    )


def downgrade() -> None:
    op.drop_table("transcription_claims")

