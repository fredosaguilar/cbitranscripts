import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load .env file
load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Automatically add any missing columns on startup
def run_safe_migrations():
    from sqlalchemy import text
    migrations = [
        # RingCentral metadata columns
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS caller_number VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS from_name VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS usage_type VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS usage_sec INTEGER",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS start_time TIMESTAMP",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS call_type VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS direction VARCHAR",
        'ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS "to_phoneNumber" VARCHAR',
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS to_name VARCHAR",
        # Audio path
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS local_audio_path VARCHAR",
        # AI analysis fields
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS insured_intent TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS material_risk_facts TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS coverage_discussed TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS monetary_values TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS options_presented TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS client_selection TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS agent_recommendation TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS eo_red_flags TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS agent_statements_liability TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS missing_information TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS confidence_score INTEGER",
        # Assignment
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS assigned_to VARCHAR",
    ]
    try:
        with engine.connect() as conn:
            for sql in migrations:
                conn.execute(text(sql))
            conn.commit()
    except Exception:
        pass

run_safe_migrations()