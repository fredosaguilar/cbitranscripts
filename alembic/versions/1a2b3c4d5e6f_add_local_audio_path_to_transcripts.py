"""add local audio path to transcripts

Revision ID: 1a2b3c4d5e6f
Revises: e6f8a4b9c2d1
Create Date: 2026-04-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "e6f8a4b9c2d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transcript_responses", sa.Column("local_audio_path", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcript_responses", "local_audio_path")
