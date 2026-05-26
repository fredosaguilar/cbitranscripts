from xml.dom.minidom import Text

from sqlalchemy import Boolean, Column, Enum, Integer, String, Text, TIMESTAMP
from database import Base
from datetime import datetime
import uuid
import enum
from sqlalchemy.dialects.postgresql import UUID


# User tokens table
class UserToken(Base):
    __tablename__ = "users_tokens"
    
    token_id = Column(String, primary_key=True, default=uuid.uuid4, unique=True, index=True)
    user_id = Column(String, nullable=False, index=True)  # store user id directly as string
    token = Column(String, nullable=False)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


#table for admin users
class Admin(Base):
    __tablename__ = "admins"

    admin_id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


class WebhookState(Base):
    __tablename__ = "webhook_state"

    state_key = Column(String, primary_key=True, default="latest_start_time")
    start_time = Column(TIMESTAMP, nullable=True)
    start_time_raw = Column(String, nullable=True)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

# Enum for status
class TranscriptStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"

class TranscriptResponse(Base):
    __tablename__ = "transcript_responses"   # ✅ renamed table

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    file_link = Column(String, nullable=False)
    owner_id = Column(String, nullable=False)

    status = Column(Enum(TranscriptStatus), default=TranscriptStatus.pending, nullable=False)

    transcription = Column(Text, nullable=True)

    client_name = Column(String, nullable=True)
    client_number = Column(String, nullable=True)
    policy_type = Column(String, nullable=True)
    reason_for_call = Column(Text, nullable=True)
    key_points = Column(Text, nullable=True)
    customer_sentiment = Column(String, nullable=True)
    follow_up_needed = Column(Boolean, nullable=True)
    follow_up_task = Column(Text, nullable=True)
    agency_zoom_task_ids = Column(Text, nullable=True)
    crm_note = Column(Text, nullable=True)
    recordingID = Column(String, nullable=True)
    local_audio_path = Column(String, nullable=True)
    caller_number = Column(String, nullable=True)
    from_name = Column(String, nullable=True)
    usage_type = Column(String, nullable=True)
    usage_sec = Column(Integer, nullable=True)
    start_time = Column(TIMESTAMP, nullable=True)
    call_type = Column(String, nullable=True)
    direction = Column(String, nullable=True)
    to_phoneNumber = Column(String, nullable=True)
    to_name = Column(String, nullable=True)
    insured_intent = Column(Text, nullable=True)
    material_risk_facts = Column(Text, nullable=True)
    coverage_discussed = Column(Text, nullable=True)
    monetary_values = Column(Text, nullable=True)
    options_presented = Column(Text, nullable=True)
    client_selection = Column(Text, nullable=True)
    agent_recommendation = Column(Text, nullable=True)
    eo_red_flags = Column(Text, nullable=True)
    agent_statements_liability = Column(Text, nullable=True)
    missing_information = Column(Text, nullable=True)
    confidence_score = Column(Integer, nullable=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow)

