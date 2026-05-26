import os
from dotenv import load_dotenv
load_dotenv()

from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Import your SQLAlchemy Base
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database import Base
import models  # your models.py

# Alembic Config object, which is correct
config = context.config

# Configure logging
fileConfig(config.config_file_name)

# Tell Alembic about your models
target_metadata = Base.metadata

# Correct way to override the DB URL from environment
db_url = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
config.set_main_option("sqlalchemy.url", db_url)

def run_migrations_offline():
    context.configure(url=db_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()