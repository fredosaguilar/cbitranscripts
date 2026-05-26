"""merge heads

Revision ID: 956c056e6f77
Revises: 1a2b3c4d5e6f, c4a7e9d8b123
Create Date: 2026-04-16 13:09:51.321760

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '956c056e6f77'
down_revision: Union[str, Sequence[str], None] = ('1a2b3c4d5e6f', 'c4a7e9d8b123')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
