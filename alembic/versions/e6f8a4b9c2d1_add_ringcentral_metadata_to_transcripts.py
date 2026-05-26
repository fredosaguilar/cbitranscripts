"""add ringcentral metadata to transcripts

Revision ID: e6f8a4b9c2d1
Revises: 7eefdafb1b46
Create Date: 2026-04-07 15:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6f8a4b9c2d1'
down_revision: Union[str, Sequence[str], None] = '7eefdafb1b46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transcript_responses', sa.Column('caller_number', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('from_name', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('usage_type', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('usage_sec', sa.Integer(), nullable=True))
    op.add_column('transcript_responses', sa.Column('start_time', sa.TIMESTAMP(), nullable=True))
    op.add_column('transcript_responses', sa.Column('call_type', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('direction', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('to_phoneNumber', sa.String(), nullable=True))
    op.add_column('transcript_responses', sa.Column('to_name', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transcript_responses', 'to_name')
    op.drop_column('transcript_responses', 'to_phoneNumber')
    op.drop_column('transcript_responses', 'direction')
    op.drop_column('transcript_responses', 'call_type')
    op.drop_column('transcript_responses', 'start_time')
    op.drop_column('transcript_responses', 'usage_sec')
    op.drop_column('transcript_responses', 'usage_type')
    op.drop_column('transcript_responses', 'from_name')
    op.drop_column('transcript_responses', 'caller_number')
