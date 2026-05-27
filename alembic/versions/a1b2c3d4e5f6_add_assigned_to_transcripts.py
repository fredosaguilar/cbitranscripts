"""add assigned_to to transcripts

Revision ID: a1b2c3d4e5f6
Revises: 956c056e6f77
Create Date: 2026-05-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '956c056e6f77'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('transcript_responses', sa.Column('assigned_to', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('transcript_responses', 'assigned_to')
