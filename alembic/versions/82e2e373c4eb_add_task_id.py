"""add task id

Revision ID: 82e2e373c4eb
Revises: 5e5c076bb6fe
Create Date: 2026-04-03 17:26:47.637429

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82e2e373c4eb'
down_revision: Union[str, Sequence[str], None] = '5e5c076bb6fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
