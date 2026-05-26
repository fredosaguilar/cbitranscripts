"""Drop users table and update users_tokens

Revision ID: 82212325d436
Revises: fa2d0f7dabb2
Create Date: 2026-03-23 18:13:23.488463
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = '82212325d436'
down_revision = 'fa2d0f7dabb2'
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Upgrade schema: drop users table, keep user_id in users_tokens as string."""
    # 1️⃣ Drop foreign key constraint first
    op.drop_constraint('users_tokens_user_id_fkey', 'users_tokens', type_='foreignkey')

    # 2️⃣ Drop index on users table if exists
    op.drop_index('ix_users_user_id', table_name='users')

    # 3️⃣ Drop users table
    op.drop_table('users')

    # 4️⃣ Alter users_tokens.user_id to be non-nullable string (already string in DB)
    op.alter_column('users_tokens', 'user_id',
               existing_type=sa.VARCHAR(),
               nullable=False)

    # 5️⃣ Create index on users_tokens.user_id for faster lookup
    op.create_index('ix_users_tokens_user_id', 'users_tokens', ['user_id'], unique=False)

def downgrade() -> None:
    """Downgrade schema: recreate users table and foreign key."""
    # 1️⃣ Drop index on users_tokens
    op.drop_index('ix_users_tokens_user_id', table_name='users_tokens')

    # 2️⃣ Alter users_tokens.user_id back to nullable
    op.alter_column('users_tokens', 'user_id',
               existing_type=sa.VARCHAR(),
               nullable=True)

    # 3️⃣ Recreate users table
    op.create_table(
        'users',
        sa.Column('user_id', sa.VARCHAR(), nullable=False),
        sa.Column('user_name', sa.VARCHAR(), nullable=False),
        sa.Column('email', sa.VARCHAR(), nullable=True),
        sa.Column('role', sa.VARCHAR(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('user_id', name='users_pkey')
    )

    # 4️⃣ Recreate index on users table
    op.create_index('ix_users_user_id', 'users', ['user_id'], unique=False)

    # 5️⃣ Recreate foreign key from users_tokens.user_id → users.user_id
    op.create_foreign_key('users_tokens_user_id_fkey', 'users_tokens', 'users', ['user_id'], ['user_id'])