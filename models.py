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
    email = Column(String, nullable=True)
    token = Column(String, nullable=True)  # Pushover key; optional when only email is used
    agency_zoom_employee_id = Column(String, nullable=True)
    agency_zoom_employee_name = Column(String, nullable=True)
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

class ReprocessRequest(Base):
    """A call waiting to be transcribed again.

    While one of these exists the scheduler cursor is held at or before the
    call's start time, so the call stays inside the fetch window until it has
    actually been reprocessed.
    """

    __tablename__ = "reprocess_requests"

    recording_id = Column(String, primary_key=True, index=True)
    start_time = Column(TIMESTAMP, nullable=True)
    requested_at = Column(TIMESTAMP, default=datetime.utcnow)


class TranscriptionAttempt(Base):
    """How many times a recording has failed to transcribe.

    After enough attempts the poor transcript is accepted so the pipeline can
    move on, rather than retrying the same recording on every sync forever.
    """

    __tablename__ = "transcription_attempts"

    recording_id = Column(String, primary_key=True, index=True)
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClientRecap(Base):
    """A recap text drafted for a client, and what became of it.

    Rows are never rewritten once sent. The value of a recap in an E&O dispute
    is being able to show exactly what wording left the office, to what number,
    and when — so a later edit becomes a new row rather than a correction to
    the record of what the client actually received.
    """

    __tablename__ = "client_recaps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    transcript_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    body = Column(Text, nullable=False)
    # The language the body is written in, and a plain English rendering of it.
    # The gloss is never sent — it is what an English reader (the agent now, an
    # adjuster later) uses to know what the client was actually told.
    language = Column(String, nullable=True)
    english_gloss = Column(Text, nullable=True)
    to_number = Column(String, nullable=True)
    from_number = Column(String, nullable=True)

    # draft | sent | failed
    status = Column(String, default="draft", nullable=False)
    # ai | template | manual — how the wording was arrived at
    source = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    provider_message_id = Column(String, nullable=True)
    provider_status = Column(String, nullable=True)
    error = Column(Text, nullable=True)

    sent_by = Column(String, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClientNoteEmail(Base):
    """The file-note email sent to a client, and what became of it.

    Kept for the same reason the recap texts are: what matters in a dispute is
    being able to show the exact words that left the office, to which address,
    and when. Rows are not rewritten once sent.
    """

    __tablename__ = "client_note_emails"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    transcript_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    to_email = Column(String, nullable=True)
    from_email = Column(String, nullable=True)
    # What the body was made from, so a note corrected afterwards refreshes it
    note_fingerprint = Column(String, nullable=True)

    # draft | sent | failed
    status = Column(String, default="draft", nullable=False)
    error = Column(Text, nullable=True)

    sent_by = Column(String, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)


class SmsOptOut(Base):
    """A number that has asked not to be texted.

    Checked before every send. Carrier-level STOP handling does not tell this
    app anything, so an opt-out heard on the phone or seen in the RingCentral
    inbox is recorded here by hand.
    """

    __tablename__ = "sms_opt_outs"

    phone_e164 = Column(String, primary_key=True, index=True)
    note = Column(Text, nullable=True)
    recorded_by = Column(String, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


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
    transcription_original = Column(Text, nullable=True)

    client_name = Column(String, nullable=True)
    client_number = Column(String, nullable=True)
    policy_type = Column(String, nullable=True)
    reason_for_call = Column(Text, nullable=True)
    key_points = Column(Text, nullable=True)
    customer_sentiment = Column(String, nullable=True)
    follow_up_needed = Column(Boolean, nullable=True)
    follow_up_task = Column(Text, nullable=True)
    agency_zoom_task_ids = Column(Text, nullable=True)
    agency_zoom_customer_id = Column(String, nullable=True)
    agency_zoom_customer_type = Column(String, nullable=True)
    agency_zoom_customer_name = Column(String, nullable=True)
    agency_zoom_due_date = Column(String, nullable=True)
    # Per follow-up task: its own due date, and the Agency Zoom task id once it
    # has been added. Keyed by the task's text, so a task that is reworded is a
    # different task rather than inheriting the old one's state.
    follow_up_task_state = Column(Text, nullable=True)
    # Set once the call write-up reaches Agency Zoom, so retrying a failed
    # approval does not post the same note a second time
    agency_zoom_note_posted_at = Column(TIMESTAMP, nullable=True)
    crm_note = Column(Text, nullable=True)
    recordingID = Column(String, nullable=True)
    local_audio_path = Column(String, nullable=True)
    caller_number = Column(String, nullable=True)
    from_name = Column(String, nullable=True)
    extension_number = Column(String, nullable=True)
    extension_name = Column(String, nullable=True)
    queue_name = Column(String, nullable=True)
    original_language = Column(String, nullable=True)
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
    assigned_to = Column(String, nullable=True)

    created_at = Column(TIMESTAMP, default=datetime.utcnow)

