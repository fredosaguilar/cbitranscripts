"""update table

Revision ID: 5e5c076bb6fe
Revises: 3c2a559dc250
Create Date: 2026-04-03 17:08:19.120537

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e5c076bb6fe'
down_revision: Union[str, Sequence[str], None] = '3c2a559dc250'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transcript_responses', sa.Column('agency_zoom_task_ids', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('transcript_responses', 'agency_zoom_task_ids')
