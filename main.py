from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import logging
import math
import os
import uuid

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
import re
import threading
import time

import requests
from typing import List, Optional
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import SessionLocal, engine
import auth
import models
from schemas import TranscriptCreate, UpdateTranscriptRequest, FollowUpTaskUpdate
from send_notification import send_push_notification
import alerts
from ringcentral_utils import (
    LOCAL_AUDIO_CACHE_DIR,
    cache_audio_file,
    delete_local_audio_file,
    fetch_audio_stream,
    get_existing_local_audio_path,
    guess_audio_content_type,
    is_ringcentral_url,
    resolve_local_audio_absolute_path,
    resolve_transcript_audio_url,
)
from zoom_agency_utils import (
    create_agency_zoom_customer_note_for_transcript,
    create_agency_zoom_tasks_for_transcript,
    normalize_follow_up_task,
    resolve_transcript_agency_zoom_match,
    search_agency_zoom_customers,
    search_agency_zoom_leads,
)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Admin Panel API")


@app.on_event("startup")
def on_startup():
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS assigned_to VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS caller_number VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS from_name VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS usage_type VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS usage_sec INTEGER",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS start_time TIMESTAMP",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS call_type VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS direction VARCHAR",
        'ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS "to_phoneNumber" VARCHAR',
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS to_name VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS local_audio_path VARCHAR",
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
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS transcription_original TEXT",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS agency_zoom_customer_id VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS agency_zoom_customer_type VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS agency_zoom_customer_name VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS extension_number VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS extension_name VARCHAR",
        "ALTER TABLE transcript_responses ADD COLUMN IF NOT EXISTS queue_name VARCHAR",
        "ALTER TABLE users_tokens ADD COLUMN IF NOT EXISTS email VARCHAR",
        # A user may be email-only, with no Pushover key
        "ALTER TABLE users_tokens ALTER COLUMN token DROP NOT NULL",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
                logging.info(f"Migration OK: {sql[:60]}")
            except Exception as e:
                logging.error(f"Migration failed: {sql[:60]} — {e}")
                conn.rollback()

UNKNOWN_VALUE = "Unknown"


def render_template(request: Request, template_name: str, context: dict | None = None, status_code: int = 200):
    context = context or {}
    context.setdefault("request", request)
    return templates.TemplateResponse(request, template_name, context, status_code=status_code)


@app.exception_handler(HTTPException)
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html and not request.url.path.startswith("/api"):
        back_url = request.headers.get("referer") or "/"
        error_message = "Page not found." if exc.status_code == 404 else exc.detail
        return render_template(
            request,
            "error.html",
            {"error_message": error_message, "back_url": back_url},
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html and not request.url.path.startswith("/api"):
        back_url = request.headers.get("referer") or "/"
        return render_template(
            request,
            "error.html",
            {
                "error_message": "Invalid form submission. Please check your fields and try again.",
                "back_url": back_url,
            },
            status_code=400,
        )
    return JSONResponse({"detail": exc.errors()}, status_code=400)
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
N8N_WEBHOOK_DELAY_SECONDS = int(os.getenv("WEBHOOK_SCHEDULER_INTERVAL_SECONDS", "60"))
WEBHOOK_SCHEDULER_ENABLED = os.getenv("WEBHOOK_SCHEDULER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Only trigger the n8n webhook during business hours to conserve n8n executions.
BUSINESS_HOURS_ENABLED = _env_flag("BUSINESS_HOURS_ENABLED")
BUSINESS_HOURS_START = int(os.getenv("BUSINESS_HOURS_START", "7"))
BUSINESS_HOURS_END = int(os.getenv("BUSINESS_HOURS_END", "19"))
# Monday=0 ... Sunday=6; default Monday-Saturday
BUSINESS_DAYS = {
    int(day) for day in (os.getenv("BUSINESS_DAYS") or "0,1,2,3,4,5").split(",") if day.strip().isdigit()
}
BUSINESS_TZ = os.getenv("BUSINESS_TZ", "America/Los_Angeles")

# Central E&O red-flag alerts and the daily digest are off by default: each
# agent is emailed about their own calls instead (see ASSIGNMENT_EMAILS_ENABLED).
RED_FLAG_ALERTS_ENABLED = _env_flag("RED_FLAG_ALERTS_ENABLED", "false")
RED_FLAG_MIN_CONFIDENCE = int(os.getenv("RED_FLAG_MIN_CONFIDENCE", "40"))

DIGEST_ENABLED = _env_flag("DIGEST_ENABLED", "false")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))

# Email the assigned agent when a call lands on their extension
ASSIGNMENT_EMAILS_ENABLED = _env_flag("ASSIGNMENT_EMAILS_ENABLED")
# Optional startup seeding: "101:henry@example.com,102:fred@example.com"
EXTENSION_EMAIL_MAP = os.getenv("EXTENSION_EMAIL_MAP", "")

# Auto-create AgencyZoom tasks for fresh calls flagged follow_up_needed
AUTO_AGENCY_ZOOM_TASKS = _env_flag("AUTO_AGENCY_ZOOM_TASKS")
AUTO_TASK_MAX_AGE_DAYS = int(os.getenv("AUTO_TASK_MAX_AGE_DAYS", "7"))

# Cached audio retention
AUDIO_CACHE_RETENTION_DAYS = int(os.getenv("AUDIO_CACHE_RETENTION_DAYS", "90"))

# Use the RingCentral extension as owner_id when the workflow reports one, so
# agents sharing a phone number are told apart. Set false to keep phone numbers.
OWNER_ID_FROM_EXTENSION = _env_flag("OWNER_ID_FROM_EXTENSION")

# Assign a new transcript to the user registered under its extension/owner id
AUTO_ASSIGN_FROM_OWNER = _env_flag("AUTO_ASSIGN_FROM_OWNER")

FRONTEND_BASE_URL = (os.getenv("FRONTEND_BASE_URL") or "").rstrip("/")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
WEBHOOK_STATE_KEY = "latest_start_time"


# Normalize optional string values before storing or comparing them.
def _clean_string(value):
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


# Extract a labeled field value from transcript text lines.
def _extract_labeled_value(transcription: str, labels):
    if not transcription:
        return None
    label_pattern = "|".join(re.escape(label) for label in labels)
    regex = re.compile(rf"^\s*(?:{label_pattern})\s*[:\-]\s*(.+)$", re.IGNORECASE)
    for line in transcription.splitlines():
        match = regex.match(line.strip())
        if match:
            return _clean_string(match.group(1))
    return None


# Find a phone number in transcript text using common caller labels.
def _extract_phone(transcription: str):
    if not transcription:
        return None
    phone_regex = re.compile(r"(\+?\d[\d\-\s\(\)]{6,}\d)")
    labels = ["caller number", "phone number", "phone", "client number"]
    for line in transcription.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        for label in labels:
            if line_stripped.lower().startswith(label):
                match = phone_regex.search(line_stripped)
                if match:
                    return _clean_string(match.group(1))
    return None


# Convert transcript follow-up intent text into a boolean flag.
def _extract_follow_up_needed(transcription: str):
    value = _extract_labeled_value(transcription, ["follow up needed", "follow-up needed"])
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"yes", "y", "true", "needed"}:
        return True
    if normalized in {"no", "n", "false", "not needed"}:
        return False
    return None


# Pull the first sentiment word from transcript text.
def _extract_sentiment(transcription: str):
    value = _extract_labeled_value(transcription, ["customer sentiment", "sentiment"])
    if not value:
        return None
    first_word = value.split()[0]
    return _clean_string(first_word.capitalize())


# Build a structured transcript payload from explicit fields or parsed text.
def _extract_structured_fields(data: TranscriptCreate):
    transcription = data.transcription or ""
    provided_sentiment = _clean_string(data.customer_sentiment)
    if provided_sentiment:
        provided_sentiment = provided_sentiment.split()[0].capitalize()

    client_name = _clean_string(data.client_name) or _extract_labeled_value(
        transcription, ["client name", "caller name", "name"]
    )
    caller_number = _clean_string(data.client_number) or _extract_phone(transcription)
    policy_type = _clean_string(data.policy_type) or _extract_labeled_value(
        transcription, ["policy type", "policy"]
    )
    reason_for_call = _clean_string(data.reason_for_call) or _extract_labeled_value(
        transcription, ["reason for call", "reason"]
    )
    key_points = _clean_string(data.key_points) or _extract_labeled_value(
        transcription, ["key points", "highlights"]
    )
    customer_sentiment = provided_sentiment or _extract_sentiment(transcription)
    follow_up_needed = (
        data.follow_up_needed
        if data.follow_up_needed is not None
        else _extract_follow_up_needed(transcription)
    )
    follow_up_task = _clean_string(data.follow_up_task) or _extract_labeled_value(
        transcription, ["follow up task", "follow-up task", "next step", "next steps"]
    )
    crm_note = _clean_string(data.crm_note) or _extract_labeled_value(
        transcription, ["crm note", "note"]
    )

    return {
        "client_name": client_name or UNKNOWN_VALUE,
        "callerNumber": caller_number or UNKNOWN_VALUE,
        "policy_type": policy_type or UNKNOWN_VALUE,
        "reason_for_call": reason_for_call or UNKNOWN_VALUE,
        "key_points": key_points or UNKNOWN_VALUE,
        "customer_sentiment": customer_sentiment or UNKNOWN_VALUE,
        "follow_up_needed": follow_up_needed if follow_up_needed is not None else False,
        "follow_up_task": follow_up_task or UNKNOWN_VALUE,
        "crm_note": crm_note or UNKNOWN_VALUE,
    }


def _format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ensure_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_utc_naive(value: datetime | None) -> datetime | None:
    normalized_value = _ensure_utc_datetime(value)
    if normalized_value is None:
        return None
    return normalized_value.replace(tzinfo=None)


def _parse_iso_datetime(value: str | None):
    cleaned_value = _clean_string(value)
    if not cleaned_value:
        return None
    try:
        parsed_value = datetime.fromisoformat(cleaned_value.replace("Z", "+00:00"))
        return _to_utc_naive(parsed_value)
    except ValueError:
        logger.warning("Invalid ISO datetime received for start_time: %s", value)
        return None


def _load_initial_webhook_start_time() -> datetime | None:
    db = SessionLocal()
    try:
        webhook_state = (
            db.query(models.WebhookState)
            .filter(models.WebhookState.state_key == WEBHOOK_STATE_KEY)
            .first()
        )
    finally:
        db.close()

    return _ensure_utc_datetime(webhook_state.start_time) if webhook_state else None


def _load_initial_webhook_start_time_raw() -> str | None:
    db = SessionLocal()
    try:
        webhook_state = (
            db.query(models.WebhookState)
            .filter(models.WebhookState.state_key == WEBHOOK_STATE_KEY)
            .first()
        )
    finally:
        db.close()

    return _clean_string(webhook_state.start_time_raw) if webhook_state else None


def _set_latest_webhook_start_time(value: datetime | None, raw_value: str | None = None):
    normalized_value = _ensure_utc_datetime(value)
    if normalized_value is None:
        return False
    cleaned_raw_value = _clean_string(raw_value) or _format_utc_timestamp(normalized_value)

    db = SessionLocal()
    try:
        webhook_state = (
            db.query(models.WebhookState)
            .filter(models.WebhookState.state_key == WEBHOOK_STATE_KEY)
            .first()
        )
        if webhook_state is None:
            webhook_state = models.WebhookState(state_key=WEBHOOK_STATE_KEY)
            db.add(webhook_state)
            current_start_time = None
        else:
            current_start_time = _ensure_utc_datetime(webhook_state.start_time)

        # Only move the scheduler cursor forward. Older or equal transcript start
        # times must not overwrite the latest webhook/db state.
        if current_start_time is not None and normalized_value <= current_start_time:
            logger.info(
                "Skipping webhook start_time update because incoming %s is not greater than current %s",
                cleaned_raw_value,
                _format_utc_timestamp(current_start_time),
            )
            return False

        webhook_state.start_time = _to_utc_naive(normalized_value)
        webhook_state.start_time_raw = cleaned_raw_value
        db.commit()
    finally:
        db.close()

    app.state.latest_webhook_start_time = normalized_value
    app.state.latest_webhook_start_time_raw = cleaned_raw_value
    return True


def _get_webhook_start_time_from_cache() -> str | None:
    cached_start_time = getattr(app.state, "latest_webhook_start_time", None)
    cached_start_time_raw = getattr(app.state, "latest_webhook_start_time_raw", None)
    cached_start_time = _ensure_utc_datetime(cached_start_time)
    if not cached_start_time:
        return None
    return cached_start_time_raw or _format_utc_timestamp(cached_start_time)


def _trigger_n8n_webhook(start_time: str):
    if not N8N_WEBHOOK_URL:
        logger.warning("Skipping n8n webhook because N8N_WEBHOOK_URL is not configured")
        return False

    payload = {"start_time": start_time}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.request(
            "GET",
            N8N_WEBHOOK_URL,
            headers=headers,
            params=payload,
            data=json.dumps(payload),
            timeout=30, 
        )
        print(f"n8n webhook response status: {response.status_code}, body: {response.text}, payload sent: {payload}")
        logger.info(
            "Triggered n8n webhook at %s with status %s and response %s",
            start_time,
            response.status_code,
            response.text,
        )
        return True
    except requests.RequestException:
        logger.warning("Failed to trigger n8n webhook for %s. Will retry in %s seconds.", start_time, N8N_WEBHOOK_DELAY_SECONDS)
        return False


def _within_business_hours() -> bool:
    if not BUSINESS_HOURS_ENABLED:
        return True
    try:
        local_now = datetime.now(ZoneInfo(BUSINESS_TZ))
    except Exception:
        logger.warning("Unknown BUSINESS_TZ %s; ignoring business-hours window", BUSINESS_TZ)
        return True
    return local_now.weekday() in BUSINESS_DAYS and BUSINESS_HOURS_START <= local_now.hour < BUSINESS_HOURS_END


def _trigger_n8n_webhook_after_delay():
    while True:
        if not _within_business_hours():
            time.sleep(N8N_WEBHOOK_DELAY_SECONDS)
            continue

        start_time = _get_webhook_start_time_from_cache()
        if start_time:
            _trigger_n8n_webhook(start_time)
        else:
            logger.info("Skipping n8n webhook because no transcript start_time is available yet")

        time.sleep(N8N_WEBHOOK_DELAY_SECONDS)



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# Provide a database session for each request lifecycle.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_logged_in_admin(request: Request, db: Session):
    token = request.cookies.get("access_token")
    if not token or not auth.SECRET_KEY:
        return None

    try:
        payload = auth.jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    except auth.JWTError:
        return None

    username = _clean_string(payload.get("sub"))
    if not username:
        return None

    return db.query(models.Admin).filter(models.Admin.username == username).first()


@app.on_event("startup")
def start_webhook_scheduler():
    if not WEBHOOK_SCHEDULER_ENABLED:
        logger.info("Webhook scheduler is disabled")
        return

    if not hasattr(app.state, "latest_webhook_start_time"):
        initial_start_time = _load_initial_webhook_start_time()
        app.state.latest_webhook_start_time = initial_start_time
        app.state.latest_webhook_start_time_raw = _load_initial_webhook_start_time_raw()
        if initial_start_time and not app.state.latest_webhook_start_time_raw:
            app.state.latest_webhook_start_time_raw = _format_utc_timestamp(initial_start_time)

    webhook_thread = getattr(app.state, "webhook_thread", None)
    if webhook_thread and webhook_thread.is_alive():
        return

    app.state.webhook_thread = threading.Thread(
        target=_trigger_n8n_webhook_after_delay,
        name="n8n-webhook-loop",
        daemon=True,
    )
    app.state.webhook_thread.start()


def _last_ten_digits(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def _reuse_known_agency_zoom_link(db: Session, transcript) -> bool:
    """Copy an Agency Zoom link from an earlier call with the same number.

    Linking a caller once should keep every later call from that number
    attached to the same customer or lead.
    """
    digits = _last_ten_digits(transcript.client_number) or _last_ten_digits(transcript.caller_number)
    if not digits:
        return False

    candidates = (
        db.query(models.TranscriptResponse)
        .filter(models.TranscriptResponse.agency_zoom_customer_id.isnot(None))
        .filter(models.TranscriptResponse.id != transcript.id)
        .filter(
            or_(
                models.TranscriptResponse.client_number.like(f"%{digits[-7:]}%"),
                models.TranscriptResponse.caller_number.like(f"%{digits[-7:]}%"),
            )
        )
        .order_by(desc(models.TranscriptResponse.created_at))
        .limit(25)
        .all()
    )

    for candidate in candidates:
        candidate_digits = _last_ten_digits(candidate.client_number) or _last_ten_digits(candidate.caller_number)
        if candidate_digits == digits:
            transcript.agency_zoom_customer_id = candidate.agency_zoom_customer_id
            transcript.agency_zoom_customer_type = candidate.agency_zoom_customer_type
            transcript.agency_zoom_customer_name = candidate.agency_zoom_customer_name
            return True
    return False


_NO_DATA_VALUES = {
    "", "none", "n/a", "na", "no", "-", "not mentioned", "none noted",
    "not applicable", "no data available", "no data available.", "nothing noted",
}


def _has_meaningful_value(value) -> bool:
    cleaned = _clean_string(value)
    return bool(cleaned) and cleaned.strip().lower() not in _NO_DATA_VALUES


def _transcript_red_flag_reasons(data: TranscriptCreate) -> list[str]:
    reasons = []
    if _has_meaningful_value(data.eo_red_flags):
        prefix = "HIGH RISK — " if "high risk" in data.eo_red_flags.lower() else ""
        reasons.append(f"{prefix}E&O red flags: {data.eo_red_flags[:400]}")
    if _has_meaningful_value(data.agent_statements_liability):
        reasons.append(f"Agent statement implying coverage/binding: {data.agent_statements_liability[:400]}")
    if data.confidence_score is not None and data.confidence_score < RED_FLAG_MIN_CONFIDENCE:
        reasons.append(f"Low analysis confidence score: {data.confidence_score} (threshold {RED_FLAG_MIN_CONFIDENCE})")
    return reasons


def _send_red_flag_alert(transcript, data: TranscriptCreate, reasons: list[str]):
    link = f"{FRONTEND_BASE_URL}/user/transcripts/{transcript.id}" if FRONTEND_BASE_URL else f"transcript id {transcript.id}"
    lines = [
        "A call was flagged during automated E&O analysis.",
        "",
        f"Client: {data.client_name or 'Unknown'}",
        f"Number: {data.client_number or data.caller_number or 'Unknown'}",
        f"Reason for call: {data.reason_for_call or 'Unknown'}",
        f"Call time: {data.start_time or 'Unknown'}",
        "",
        "Flags:",
    ]
    lines += [f"  - {reason}" for reason in reasons]
    lines += ["", f"Review: {link}"]
    alerts.send_email_async(
        subject=f"E&O ALERT: {data.client_name or data.caller_number or 'Unknown caller'}",
        body_text="\n".join(lines),
    )


def _send_assignment_email(transcript, user_email: str):
    """Tell the agent a call landed on their extension and needs review."""
    link = f"{FRONTEND_BASE_URL}/user/transcripts/{transcript.id}" if FRONTEND_BASE_URL else str(transcript.id)
    client = _clean_string(transcript.client_name) or _clean_string(transcript.client_number) or "Unknown caller"
    lines = [
        f"A call assigned to you is ready for review: {client}",
        "",
        f"Client: {client}",
        f"Number: {_clean_string(transcript.client_number) or 'Unknown'}",
        f"Reason for call: {_clean_string(transcript.reason_for_call) or 'Not identified'}",
        f"Call time: {transcript.start_time or 'Unknown'}",
    ]
    if transcript.queue_name:
        lines.append(f"Arrived via: {transcript.queue_name}")
    follow_up = _clean_string(transcript.follow_up_task)
    if follow_up:
        lines += ["", "Follow-up noted on the call:", follow_up]
    lines += [
        "",
        "Please review the transcription and the notes, correct anything the AI got wrong,",
        "and approve the transcript when it is accurate.",
        "",
        f"Open the call: {link}",
    ]

    alerts.send_email_async(
        subject=f"Review your call: {client}",
        body_text="\n".join(lines),
        to_addresses=[user_email],
    )


def _seed_extension_emails():
    """Create or update user records from EXTENSION_EMAIL_MAP at startup."""
    entries = [entry.strip() for entry in EXTENSION_EMAIL_MAP.split(",") if entry.strip()]
    if not entries:
        return

    db = SessionLocal()
    try:
        for entry in entries:
            if ":" not in entry:
                logger.warning("Ignoring malformed EXTENSION_EMAIL_MAP entry: %s", entry)
                continue
            extension, email = (part.strip() for part in entry.split(":", 1))
            if not extension or not email:
                continue
            user = db.query(models.UserToken).filter(models.UserToken.user_id == extension).first()
            if user:
                if user.email != email:
                    user.email = email
                    logger.info("Updated email for extension %s", extension)
            else:
                db.add(models.UserToken(token_id=str(uuid.uuid4()), user_id=extension, email=email))
                logger.info("Registered extension %s for assignment emails", extension)
        db.commit()
    except Exception:
        logger.exception("Failed to seed extension emails")
    finally:
        db.close()


def _local_tz():
    try:
        return ZoneInfo(BUSINESS_TZ)
    except Exception:
        return timezone.utc


def _build_daily_digest() -> tuple[str, str] | None:
    tz = _local_tz()
    local_now = datetime.now(tz)
    day_start_local = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    start_utc = day_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = day_end_local.astimezone(timezone.utc).replace(tzinfo=None)

    db = SessionLocal()
    try:
        rows = (
            db.query(models.TranscriptResponse)
            .filter(
                models.TranscriptResponse.created_at >= start_utc,
                models.TranscriptResponse.created_at < end_utc,
            )
            .order_by(models.TranscriptResponse.start_time.desc().nullslast())
            .all()
        )
        unassigned_total = (
            db.query(func.count(models.TranscriptResponse.id))
            .filter(models.TranscriptResponse.assigned_to.is_(None))
            .scalar()
        ) or 0
    finally:
        db.close()

    flagged = [r for r in rows if _has_meaningful_value(r.eo_red_flags)]
    follow_ups = [r for r in rows if r.follow_up_needed]

    day_label = day_start_local.strftime("%A, %B %d")
    lines = [
        f"Daily call digest for {day_label}",
        "",
        f"Calls processed: {len(rows)}",
        f"Follow-ups needed: {len(follow_ups)}",
        f"E&O red flags: {len(flagged)}",
        f"Unassigned transcripts (all time): {unassigned_total}",
    ]

    def _entry(row) -> str:
        name = row.client_name or row.caller_number or "Unknown"
        link = f"{FRONTEND_BASE_URL}/user/transcripts/{row.id}" if FRONTEND_BASE_URL else str(row.id)
        return f"  - {name} ({row.reason_for_call or 'Unknown reason'}) — {link}"

    if flagged:
        lines += ["", "Red-flagged calls:"] + [_entry(r) for r in flagged[:20]]
    if follow_ups:
        lines += ["", "Follow-ups needed:"] + [_entry(r) for r in follow_ups[:20]]

    return f"Daily digest — {day_label}: {len(rows)} calls, {len(flagged)} flagged", "\n".join(lines)


def _daily_digest_loop():
    while True:
        tz = _local_tz()
        local_now = datetime.now(tz)
        next_run = local_now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if next_run <= local_now:
            next_run += timedelta(days=1)
        time.sleep(max((next_run - local_now).total_seconds(), 60))
        try:
            digest = _build_daily_digest()
            if digest:
                alerts.send_email(digest[0], digest[1])
        except Exception:
            logger.exception("Failed to send daily digest")


def _cleanup_audio_cache_once():
    cache_dir = os.path.abspath(LOCAL_AUDIO_CACHE_DIR)
    if not os.path.isdir(cache_dir):
        return
    cutoff = time.time() - AUDIO_CACHE_RETENTION_DAYS * 86400
    removed = 0
    for filename in os.listdir(cache_dir):
        path = os.path.join(cache_dir, filename)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            logger.warning("Could not remove cached audio file %s", path)
    if removed:
        logger.info("Audio cache cleanup removed %s file(s) older than %s days", removed, AUDIO_CACHE_RETENTION_DAYS)

    # Clear stale local_audio_path references so playback falls back to RingCentral
    db = SessionLocal()
    try:
        rows = (
            db.query(models.TranscriptResponse)
            .filter(models.TranscriptResponse.local_audio_path.isnot(None))
            .all()
        )
        changed = False
        for row in rows:
            if not get_existing_local_audio_path(row.local_audio_path):
                row.local_audio_path = None
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def _audio_cache_cleanup_loop():
    while True:
        try:
            _cleanup_audio_cache_once()
        except Exception:
            logger.exception("Audio cache cleanup failed")
        time.sleep(86400)


@app.on_event("startup")
def bootstrap_admin_account():
    # One-time admin recovery: set ADMIN_BOOTSTRAP_USERNAME and
    # ADMIN_BOOTSTRAP_PASSWORD in the environment, deploy, log in, then
    # remove both variables.
    username = _clean_string(os.getenv("ADMIN_BOOTSTRAP_USERNAME"))
    password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD") or ""
    if not username or len(password) < 8:
        return

    db = SessionLocal()
    try:
        admin = db.query(models.Admin).filter(models.Admin.username == username).first()
        if admin:
            admin.password_hash = auth.hash_password(password)
            logger.info("Bootstrap: reset password for admin '%s'", username)
        else:
            db.add(models.Admin(
                admin_id=str(uuid.uuid4()),
                username=username,
                password_hash=auth.hash_password(password),
            ))
            logger.info("Bootstrap: created admin '%s'", username)
        db.commit()
    except Exception:
        logger.exception("Bootstrap admin failed")
    finally:
        db.close()


@app.on_event("startup")
def seed_extension_emails_on_startup():
    _seed_extension_emails()


@app.on_event("startup")
def start_background_jobs():
    if DIGEST_ENABLED and alerts.is_email_configured():
        thread = threading.Thread(target=_daily_digest_loop, name="daily-digest", daemon=True)
        thread.start()
    elif DIGEST_ENABLED:
        logger.info("Daily digest enabled but email is not configured; set SMTP_* and ALERT_EMAIL_TO")

    threading.Thread(target=_audio_cache_cleanup_loop, name="audio-cache-cleanup", daemon=True).start()


@app.get("/", response_class=HTMLResponse)
# Render the admin login page.
def login_page(request: Request):
    context = {"request": request, "error": None}
    return templates.TemplateResponse(request, "login.html", context)


@app.post("/admin/login")
# Authenticate an admin user and start the dashboard session.
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = auth.authenticate_admin(db, username, password)
    if not admin:
        context = {"request": request, "error": "Invalid username or password"}
        return templates.TemplateResponse(request, "login.html", context, status_code=401)

    access_token = auth.create_access_token(data={"sub": admin.username})
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(key="access_token", value=access_token)
    return response


@app.get("/logout")
# Clear the admin session cookie and return to the login page.
def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(key="access_token")
    return response


@app.get("/admin/dashboard")
# Render the dashboard with all registered notification users.
def dashboard(request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    users_tokens = db.query(models.UserToken).all()
    registered_ids = {token.user_id for token in users_tokens}

    # Extensions seen on recent calls, so each one can be mapped to an agent
    extension_rows = (
        db.query(
            models.TranscriptResponse.extension_number,
            models.TranscriptResponse.extension_name,
            func.count(models.TranscriptResponse.id).label("call_count"),
        )
        .filter(models.TranscriptResponse.extension_number.isnot(None))
        .group_by(
            models.TranscriptResponse.extension_number,
            models.TranscriptResponse.extension_name,
        )
        .order_by(desc("call_count"))
        .all()
    )
    seen_extensions = [
        {
            "number": row.extension_number,
            "name": row.extension_name or "",
            "calls": row.call_count,
            "registered": row.extension_number in registered_ids,
        }
        for row in extension_rows
    ]

    context = {
        "request": request,
        "users_tokens": users_tokens,
        "seen_extensions": seen_extensions,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@app.get("/admin/add-user", response_class=HTMLResponse)
# Render the add-user form page.
def add_user_page(request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(request, "add_user.html", {"request": request})


@app.post("/admin/add-user")
# Save a new notification user token.
def add_user(
    request: Request,
    user_id: str = Form(...),
    email: str = Form(""),
    user_token: str = Form(""),
    db: Session = Depends(get_db),
):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    new_user = models.UserToken(
        token_id=str(uuid.uuid4()),
        user_id=_clean_string(user_id),
        email=_clean_string(email),
        token=_clean_string(user_token),
    )
    db.add(new_user)
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/admin/delete-user/{token_id}")
# Delete a stored notification user token.
def delete_user(token_id: str, db: Session = Depends(get_db)):
    user = db.query(models.UserToken).filter(models.UserToken.token_id == token_id).first()
    if user:
        db.delete(user)
        db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/admin/edit-user/{token_id}", response_class=HTMLResponse)
# Render the edit page for a stored user token.
def edit_user_page(token_id: str, request: Request, db: Session = Depends(get_db)):
    user = db.query(models.UserToken).filter(models.UserToken.token_id == token_id).first()
    return templates.TemplateResponse(request, "edit_user.html", {"request": request, "user": user})


@app.post("/admin/edit-user/{token_id}")
# Update an existing notification user token.
def update_user(
    token_id: str,
    email: str = Form(""),
    user_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.query(models.UserToken).filter(models.UserToken.token_id == token_id).first()
    if user:
        user.email = _clean_string(email)
        user.token = _clean_string(user_token)
        db.commit()
    return RedirectResponse(url="/admin/dashboard?updated=1", status_code=303)


@app.get("/admin/transcripts")
# Render the transcript list for admin review, with server-side filters and pagination.
def list_transcripts(request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    params = request.query_params
    q = _clean_string(params.get("q"))
    status_filter = _clean_string(params.get("status"))
    assigned_filter = _clean_string(params.get("assigned_to"))
    date_from = _clean_string(params.get("date_from"))
    date_to = _clean_string(params.get("date_to"))
    try:
        page = max(int(params.get("page") or 1), 1)
    except ValueError:
        page = 1
    try:
        per_page = min(max(int(params.get("per_page") or 50), 10), 200)
    except ValueError:
        per_page = 50

    query = db.query(models.TranscriptResponse)
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                models.TranscriptResponse.client_name.ilike(like),
                models.TranscriptResponse.client_number.ilike(like),
                models.TranscriptResponse.caller_number.ilike(like),
                models.TranscriptResponse.from_name.ilike(like),
                models.TranscriptResponse.recordingID.ilike(like),
                models.TranscriptResponse.owner_id.ilike(like),
                models.TranscriptResponse.agency_zoom_customer_name.ilike(like),
            )
        )
    if status_filter in {status.value for status in models.TranscriptStatus}:
        query = query.filter(models.TranscriptResponse.status == status_filter)
    if assigned_filter == "unassigned":
        query = query.filter(models.TranscriptResponse.assigned_to.is_(None))
    elif assigned_filter:
        query = query.filter(models.TranscriptResponse.assigned_to == assigned_filter)
    parsed_from = _parse_iso_datetime(date_from)
    if parsed_from:
        query = query.filter(models.TranscriptResponse.start_time >= parsed_from)
    parsed_to = _parse_iso_datetime(date_to)
    if parsed_to:
        query = query.filter(models.TranscriptResponse.start_time < parsed_to + timedelta(days=1))

    total_filtered = query.count()
    pages = max(math.ceil(total_filtered / per_page), 1)
    page = min(page, pages)
    transcripts = (
        query.order_by(
            models.TranscriptResponse.start_time.desc().nullslast(),
            desc(models.TranscriptResponse.created_at),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    total_all = db.query(func.count(models.TranscriptResponse.id)).scalar() or 0
    approved_count = (
        db.query(func.count(models.TranscriptResponse.id))
        .filter(models.TranscriptResponse.status == models.TranscriptStatus.approved.value)
        .scalar()
    ) or 0
    pending_count = total_all - approved_count

    users = db.query(models.UserToken.user_id).distinct().all()
    user_ids = sorted([u.user_id for u in users])

    filter_params = {
        "q": q or "",
        "status": status_filter or "",
        "assigned_to": assigned_filter or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
        "per_page": per_page,
    }
    base_query_string = "&".join(
        f"{key}={value}" for key, value in filter_params.items() if value not in ("", None)
    )

    return templates.TemplateResponse(request, "transcripts.html", {
        "request": request,
        "transcripts": transcripts,
        "user_ids": user_ids,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total_filtered": total_filtered,
        "total_all": total_all,
        "approved_count": approved_count,
        "pending_count": pending_count,
        "filters": filter_params,
        "base_query_string": base_query_string,
        "row_offset": (page - 1) * per_page,
    })


@app.get("/admin/analytics")
# Render call analytics: volume, sentiment, red flags, and per-line breakdowns.
def analytics_page(request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    try:
        days = min(max(int(request.query_params.get("days") or 30), 7), 365)
    except ValueError:
        days = 30
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    rows = (
        db.query(models.TranscriptResponse)
        .filter(
            or_(
                models.TranscriptResponse.start_time >= cutoff,
                models.TranscriptResponse.start_time.is_(None),
            ),
            models.TranscriptResponse.created_at >= cutoff,
        )
        .all()
    )

    daily_counts: dict[str, int] = {}
    sentiment_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    owner_counts: dict[str, int] = {}
    flagged_count = 0
    follow_up_count = 0
    total_seconds = 0

    for row in rows:
        stamp = row.start_time or row.created_at
        if stamp:
            daily_counts[stamp.strftime("%Y-%m-%d")] = daily_counts.get(stamp.strftime("%Y-%m-%d"), 0) + 1
        sentiment = (_clean_string(row.customer_sentiment) or "Unknown").capitalize()
        sentiment_counts[sentiment] = sentiment_counts.get(sentiment, 0) + 1
        reason = _clean_string(row.reason_for_call) or "Unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        owner = _clean_string(row.owner_id) or "Unknown"
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        if _has_meaningful_value(row.eo_red_flags):
            flagged_count += 1
        if row.follow_up_needed:
            follow_up_count += 1
        if row.usage_sec:
            total_seconds += row.usage_sec

    daily_series = sorted(daily_counts.items())
    max_daily = max(daily_counts.values(), default=1)

    def _sorted_counts(counts: dict[str, int], limit: int = 10):
        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
        peak = max((count for _, count in ordered), default=1)
        return [(label, count, round(count * 100 / peak)) for label, count in ordered]

    return templates.TemplateResponse(request, "analytics.html", {
        "request": request,
        "days": days,
        "total_calls": len(rows),
        "flagged_count": flagged_count,
        "follow_up_count": follow_up_count,
        "total_minutes": round(total_seconds / 60),
        "daily_series": [(day, count, round(count * 100 / max_daily)) for day, count in daily_series],
        "sentiment_counts": _sorted_counts(sentiment_counts),
        "reason_counts": _sorted_counts(reason_counts),
        "owner_counts": _sorted_counts(owner_counts),
    })


@app.get("/admin/admins")
# Render the admin account management page.
def admins_page(request: Request, db: Session = Depends(get_db)):
    current_admin = get_logged_in_admin(request, db)
    if not current_admin:
        return RedirectResponse(url="/", status_code=303)

    admins = db.query(models.Admin).order_by(models.Admin.username).all()
    return templates.TemplateResponse(request, "admins.html", {
        "request": request,
        "admins": admins,
        "current_admin": current_admin,
        "error": request.query_params.get("error"),
        "created": request.query_params.get("created"),
    })


@app.post("/admin/admins/add")
# Create an additional admin login.
def add_admin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    cleaned_username = _clean_string(username)
    if not cleaned_username or len(password) < 8:
        return RedirectResponse(
            url="/admin/admins?error=Username+required+and+password+must+be+at+least+8+characters",
            status_code=303,
        )
    if db.query(models.Admin).filter(models.Admin.username == cleaned_username).first():
        return RedirectResponse(url="/admin/admins?error=Username+already+exists", status_code=303)

    db.add(models.Admin(
        admin_id=str(uuid.uuid4()),
        username=cleaned_username,
        password_hash=auth.hash_password(password),
    ))
    db.commit()
    return RedirectResponse(url="/admin/admins?created=1", status_code=303)


@app.post("/admin/admins/{admin_id}/delete")
# Delete an admin login (cannot delete yourself or the last remaining admin).
def delete_admin(admin_id: str, request: Request, db: Session = Depends(get_db)):
    current_admin = get_logged_in_admin(request, db)
    if not current_admin:
        return RedirectResponse(url="/", status_code=303)

    if current_admin.admin_id == admin_id:
        return RedirectResponse(url="/admin/admins?error=You+cannot+delete+your+own+account", status_code=303)
    if (db.query(func.count(models.Admin.admin_id)).scalar() or 0) <= 1:
        return RedirectResponse(url="/admin/admins?error=Cannot+delete+the+last+admin", status_code=303)

    target = db.query(models.Admin).filter(models.Admin.admin_id == admin_id).first()
    if target:
        db.delete(target)
        db.commit()
    return RedirectResponse(url="/admin/admins", status_code=303)


@app.post("/admin/transcripts/bulk-delete")
# Delete multiple transcript records at once.
def bulk_delete_transcripts(request: Request, ids: Optional[List[str]] = Form(None), db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    if ids:
        for id in ids:
            transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
            if transcript:
                delete_local_audio_file(getattr(transcript, "local_audio_path", None))
                db.delete(transcript)
        db.commit()

    return RedirectResponse(url="/admin/transcripts", status_code=303)


@app.post("/admin/transcripts/{id}/assign")
# Assign a transcript to a user.
def assign_transcript(id: str, assigned_to: str = Form(""), db: Session = Depends(get_db)):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    if transcript:
        transcript.assigned_to = assigned_to if assigned_to else None
        db.commit()
    return JSONResponse(content={"status": "ok"})


@app.get("/api/transcripts/{id}/agencyzoom/search")
# Search Agency Zoom customers and leads for a transcript, by phone or name.
def agency_zoom_search(id: str, request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")

    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    explicit_query = _clean_string(request.query_params.get("q"))
    if explicit_query:
        attempts = [(explicit_query, "your search")]
    else:
        # Widen automatically: phone first, then the names we know for the call
        attempts = [
            (_clean_string(transcript.client_number), "client number"),
            (_clean_string(transcript.caller_number), "caller number"),
            (_clean_string(transcript.client_name), "client name"),
            (_clean_string(transcript.from_name), "caller name"),
        ]
        attempts = [(value, label) for value, label in attempts if value]

    if not attempts:
        return JSONResponse(content={"query": None, "query_label": None, "results": []})

    call_digits = _last_ten_digits(transcript.client_number) or _last_ten_digits(transcript.caller_number)
    used_query = attempts[0][0]
    used_label = attempts[0][1]
    unique_results: list[dict] = []

    for value, label in attempts:
        try:
            found = search_agency_zoom_customers(value) + search_agency_zoom_leads(value)
        except Exception as exc:
            logger.exception("Agency Zoom search failed for transcript %s", id)
            raise HTTPException(status_code=502, detail=f"Agency Zoom search failed: {exc}") from exc

        seen: set[str] = set()
        unique_results = []
        for result in found:
            key = f"{result.get('type')}:{result.get('id')}"
            if result.get("id") and key not in seen:
                seen.add(key)
                # Flag records whose phone matches this call exactly
                result["exact_phone"] = bool(
                    call_digits and _last_ten_digits(result.get("phone")) == call_digits
                )
                unique_results.append(result)

        if unique_results:
            used_query, used_label = value, label
            break

    # Exact phone matches first, then customers before leads, then by name
    unique_results.sort(
        key=lambda record: (
            not record.get("exact_phone"),
            record.get("type") != "customer",
            (record.get("name") or "").lower(),
        )
    )

    return JSONResponse(content={
        "query": used_query,
        "query_label": used_label,
        "results": unique_results,
    })


@app.post("/api/transcripts/{id}/agencyzoom/link")
# Link a transcript to a specific Agency Zoom customer or lead.
def agency_zoom_link(
    id: str,
    request: Request,
    customer_id: str = Form(""),
    customer_type: str = Form("customer"),
    customer_name: str = Form(""),
    apply_to_number: str = Form(""),
    db: Session = Depends(get_db),
):
    if not get_logged_in_admin(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")

    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    # An empty customer_id clears the link
    linked_id = _clean_string(customer_id)
    linked_type = (_clean_string(customer_type) or "customer") if linked_id else None
    linked_name = _clean_string(customer_name) if linked_id else None

    transcript.agency_zoom_customer_id = linked_id
    transcript.agency_zoom_customer_type = linked_type
    transcript.agency_zoom_customer_name = linked_name

    # Optionally apply the same link to every other call from this number
    also_updated = 0
    if apply_to_number.strip().lower() in {"1", "true", "yes", "on"}:
        digits = _last_ten_digits(transcript.client_number) or _last_ten_digits(transcript.caller_number)
        if digits:
            siblings = (
                db.query(models.TranscriptResponse)
                .filter(models.TranscriptResponse.id != transcript.id)
                .filter(
                    or_(
                        models.TranscriptResponse.client_number.like(f"%{digits[-7:]}%"),
                        models.TranscriptResponse.caller_number.like(f"%{digits[-7:]}%"),
                    )
                )
                .all()
            )
            for sibling in siblings:
                sibling_digits = _last_ten_digits(sibling.client_number) or _last_ten_digits(sibling.caller_number)
                if sibling_digits != digits:
                    continue
                sibling.agency_zoom_customer_id = linked_id
                sibling.agency_zoom_customer_type = linked_type
                sibling.agency_zoom_customer_name = linked_name
                also_updated += 1

    db.commit()

    return JSONResponse(content={
        "status": "ok",
        "linked": bool(transcript.agency_zoom_customer_id),
        "customer_id": transcript.agency_zoom_customer_id,
        "customer_type": transcript.agency_zoom_customer_type,
        "customer_name": transcript.agency_zoom_customer_name,
        "also_updated": also_updated,
    })


@app.post("/admin/transcripts/{id}/delete")
# Delete a transcript record.
def delete_transcript(id: str, db: Session = Depends(get_db)):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()

    if transcript:
        delete_local_audio_file(getattr(transcript, "local_audio_path", None))
        db.delete(transcript)
        db.commit()

    return RedirectResponse(url="/admin/transcripts", status_code=303)


@app.post("/api/transcripts")
# Store a new transcript and notify the assigned user.
def create_transcript(
    data: TranscriptCreate,
    db: Session = Depends(get_db),
):
    is_outbound = (data.direction or "").strip().lower() == "outbound"
    # On a shared line every call carries the same phone number, so the extension
    # that handled it is what actually identifies the agent.
    extension_number = _clean_string(data.extension_number)
    if OWNER_ID_FROM_EXTENSION and extension_number:
        resolved_owner_id = extension_number
    else:
        resolved_owner_id = _clean_string(data.caller_number) if is_outbound else _clean_string(data.owner_id) # default  owner id   assume the call is inbound
        if not resolved_owner_id:
            resolved_owner_id = _clean_string(data.owner_id) or _clean_string(data.to_phoneNumber) or UNKNOWN_VALUE

    
    # handle the client  number

    if is_outbound:
        resolved_client_number = _clean_string(data.to_phoneNumber) 
    else:
        resolved_client_number = _clean_string(data.caller_number) or _clean_string(data.client_number) 

    normalized_recording_id = _clean_string(data.recordingID)

    if normalized_recording_id:
        existing_transcript = (
            db.query(models.TranscriptResponse)
            .filter(models.TranscriptResponse.recordingID == normalized_recording_id)
            .first()
        )
        if existing_transcript:
            return JSONResponse(
                content={
                    "message": "Transcript with this recordingID already exists",
                    "id": str(existing_transcript.id),
                    "recordingID": existing_transcript.recordingID,
                    "duplicate": True,
                },
                status_code=200,
            )

    new_transcript = models.TranscriptResponse(
        file_link=data.file_link,
        owner_id=resolved_owner_id,
        transcription=data.transcription,
        transcription_original=data.transcription_original,
        client_name=data.client_name,
        client_number=resolved_client_number,
        policy_type=data.policy_type,
        reason_for_call=data.reason_for_call,
        key_points=data.key_points,
        customer_sentiment=data.customer_sentiment,
        follow_up_needed=data.follow_up_needed,
        follow_up_task=data.follow_up_task,
        crm_note=data.crm_note,
        recordingID=normalized_recording_id,
        caller_number=data.caller_number,
        from_name=data.from_name,
        extension_number=extension_number,
        extension_name=data.extension_name,
        queue_name=data.queue_name,
        usage_type=data.usage_type,
        usage_sec=data.usage_sec,
        start_time=_parse_iso_datetime(data.start_time),
        call_type=data.call_type,
        direction=data.direction,
        to_phoneNumber=data.to_phoneNumber,
        to_name=data.to_name,
        insured_intent=data.insured_intent,
        material_risk_facts=data.material_risk_facts,
        coverage_discussed=data.coverage_discussed,
        monetary_values=data.monetary_values,
        options_presented=data.options_presented,
        client_selection=data.client_selection,
        agent_recommendation=data.agent_recommendation,
        eo_red_flags=data.eo_red_flags,
        agent_statements_liability=data.agent_statements_liability,
        missing_information=data.missing_information,
        confidence_score=data.confidence_score,
    )

    # If this caller was matched to an Agency Zoom record on an earlier call,
    # carry that link forward instead of re-searching.
    try:
        _reuse_known_agency_zoom_link(db, new_transcript)
    except Exception:
        logger.exception("Could not reuse a previous Agency Zoom link for %s", new_transcript.recordingID)

    # Auto-assign to the user registered for this extension (or owner id), so
    # calls land on the right agent's plate without manual triage.
    assigned_user = None
    if AUTO_ASSIGN_FROM_OWNER and not new_transcript.assigned_to:
        for candidate in (extension_number, resolved_owner_id):
            if not candidate:
                continue
            registered_user = (
                db.query(models.UserToken)
                .filter(models.UserToken.user_id == candidate)
                .first()
            )
            if registered_user:
                new_transcript.assigned_to = registered_user.user_id
                assigned_user = registered_user
                break

    db.add(new_transcript)
    db.commit()
    db.refresh(new_transcript)
    _set_latest_webhook_start_time(new_transcript.start_time, data.start_time)

    if RED_FLAG_ALERTS_ENABLED:
        try:
            red_flag_reasons = _transcript_red_flag_reasons(data)
            if red_flag_reasons:
                _send_red_flag_alert(new_transcript, data, red_flag_reasons)
        except Exception:
            logger.exception("Failed to send red-flag alert for transcript %s", new_transcript.id)

    # Auto-create AgencyZoom follow-up tasks for fresh calls only, so backlog
    # re-sweeps of old calls never spam the CRM.
    if AUTO_AGENCY_ZOOM_TASKS and new_transcript.follow_up_needed and not new_transcript.agency_zoom_task_ids:
        call_time = _ensure_utc_datetime(new_transcript.start_time)
        is_recent = call_time is not None and (
            datetime.now(timezone.utc) - call_time <= timedelta(days=AUTO_TASK_MAX_AGE_DAYS)
        )
        if is_recent and normalize_follow_up_task(new_transcript.follow_up_task):
            try:
                created_task_ids = create_agency_zoom_tasks_for_transcript(new_transcript)
                if created_task_ids:
                    new_transcript.agency_zoom_task_ids = "\n".join(created_task_ids)
                    db.commit()
            except Exception:
                logger.exception("Failed to auto-create AgencyZoom tasks for transcript %s", new_transcript.id)

    # Tell the assigned agent their call is ready to review
    if ASSIGNMENT_EMAILS_ENABLED:
        recipient = assigned_user or (
            db.query(models.UserToken)
            .filter(models.UserToken.user_id == new_transcript.assigned_to)
            .first()
            if new_transcript.assigned_to
            else None
        )
        recipient_email = _clean_string(getattr(recipient, "email", None))
        if recipient_email:
            try:
                _send_assignment_email(new_transcript, recipient_email)
            except Exception:
                logger.exception("Failed to send assignment email for transcript %s", new_transcript.id)

    data.owner_id = resolved_owner_id
    data.client_number = resolved_client_number
    structured_response = _extract_structured_fields(data)
    user_token = db.query(models.UserToken).filter(models.UserToken.user_id == new_transcript.owner_id).first()
    if user_token and _clean_string(user_token.token):
        send_push_notification(str(new_transcript.id), user_token.token, structured_response)

    return JSONResponse(content=structured_response, status_code=200)


@app.get("/api/transcripts/check-recording/{recording_id}")
# Check whether a transcript already exists for the given recording ID.
def check_recording_id_exists(
    recording_id: str,
    db: Session = Depends(get_db),
):
    normalized_recording_id = _clean_string(recording_id)
    if not normalized_recording_id:
        raise HTTPException(status_code=400, detail="recording_id is required")

    transcript = (
        db.query(models.TranscriptResponse)
        .filter(models.TranscriptResponse.recordingID == normalized_recording_id)
        .first()
    )

    return JSONResponse(
        content={
            "recordingID": normalized_recording_id,
            "exists": transcript is not None,
            "id": str(transcript.id) if transcript else None,
        },
        status_code=200,
    )


@app.get("/user/transcripts/{id}")
# Render the transcript detail page for a single record.
def transcript_detail(id: str, request: Request, db: Session = Depends(get_db)):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    error_message = request.query_params.get("error")
    return render_template(
        request,
        "transcript_detail.html",
        {"transcript": transcript, "error_message": error_message},
    )


@app.get("/api/transcripts/{id}/agencyzoom/status")
# Report whether this transcript resolves to an Agency Zoom customer or lead.
def agency_zoom_status(id: str, request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")

    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    try:
        match = resolve_transcript_agency_zoom_match(transcript)
    except Exception:
        logger.exception("Agency Zoom match lookup failed for transcript %s", id)
        return JSONResponse(content={"match": None, "error": "Agency Zoom lookup failed"}, status_code=200)

    return JSONResponse(content={"match": match})


@app.get("/api/transcripts/{id}/audio")
# Stream the transcript audio file, adding RingCentral auth when configured.
def stream_transcript_audio(id: str, request: Request, db: Session = Depends(get_db)):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    cached_audio_path = get_existing_local_audio_path(getattr(transcript, "local_audio_path", None))
    if cached_audio_path:
        return FileResponse(
            cached_audio_path,
            media_type=guess_audio_content_type(cached_audio_path),
            filename=os.path.basename(cached_audio_path),
        )

    audio_url = resolve_transcript_audio_url(transcript)
    if not audio_url:
        raise HTTPException(
            status_code=404,
            detail="Audio file is not available for this transcript. Add file_link or configure RingCentral account playback from recordingID.",
        )

    byte_range = request.headers.get("range")

    try:
        relative_audio_path, content_type = cache_audio_file(transcript, audio_url)
        transcript.local_audio_path = relative_audio_path
        db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        if is_ringcentral_url(audio_url):
            detail = "Unable to fetch RingCentral audio. Check RingCentral account, token refresh settings, and recording access."
        else:
            detail = f"Unable to fetch audio file from upstream source (status {status_code})."
        raise HTTPException(status_code=502, detail=detail) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Unable to connect to the upstream audio source.") from exc

    cached_audio_path = resolve_local_audio_absolute_path(relative_audio_path)
    if not cached_audio_path or not os.path.exists(cached_audio_path):
        raise HTTPException(status_code=500, detail="Audio was downloaded but the local cache file could not be found.")

    if byte_range:
        return StreamingResponse(
            fetch_audio_stream(audio_url, byte_range=byte_range).iter_content(chunk_size=1024 * 64),
            media_type=content_type,
        )

    return FileResponse(
        cached_audio_path,
        media_type=content_type,
        filename=os.path.basename(cached_audio_path),
    )


@app.post("/user/transcripts/{id}/update")
# Update transcript status and trigger Agency Zoom actions on approval.
def update_status(
    id: str,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()

    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    previous_status = transcript.status.value if hasattr(transcript.status, "value") else str(transcript.status)
    normalized_tasks = normalize_follow_up_task(transcript.follow_up_task)

    if status == models.TranscriptStatus.approved.value and previous_status != models.TranscriptStatus.approved.value:
        if normalized_tasks:
            # Skip creation when tasks were already auto-created at ingest time
            if not _clean_string(transcript.agency_zoom_task_ids):
                created_task_ids = create_agency_zoom_tasks_for_transcript(transcript)
                transcript.agency_zoom_task_ids = "\n".join(created_task_ids) if created_task_ids else None
        else:
            if not _clean_string(transcript.crm_note):
                return render_template(
                    request,
                    "transcript_detail.html",
                    {
                        "transcript": transcript,
                        "error_message": "CRM note is required to approve this transcript.",
                    },
                    status_code=400,
                )
            create_agency_zoom_customer_note_for_transcript(transcript)
            transcript.agency_zoom_task_ids = None

    transcript.status = status
    db.commit()

    return RedirectResponse(url=f"/user/transcripts/{id}", status_code=303)


@app.put("/api/transcripts/{id}/transcription")
# Update editable transcript analysis fields.
def update_transcription(
    id: str,
    data: UpdateTranscriptRequest,
    db: Session = Depends(get_db),
):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()

    if not transcript:
        raise HTTPException(status_code=404, detail="Transcript not found")

    if data.transcription is not None:
        transcript.transcription = data.transcription
    if data.reason_for_call is not None:
        transcript.reason_for_call = data.reason_for_call
    if data.key_points is not None:
        transcript.key_points = data.key_points
    if data.follow_up_task is not None:
        transcript.follow_up_task = data.follow_up_task
    if data.crm_note is not None:
        transcript.crm_note = data.crm_note

    db.commit()
    db.refresh(transcript)

    return {
        "message": "Transcription updated successfully",
        "id": str(transcript.id),
        "transcription": transcript.transcription,
        "reason_for_call": transcript.reason_for_call,
        "key_points": transcript.key_points,
        "follow_up_task": transcript.follow_up_task,
        "crm_note": transcript.crm_note,
    }


@app.post("/api/transcripts/{id}/follow-up")
# Add, edit, or remove follow-up tasks for a transcript.
def update_follow_up_task(
    id: str,
    data: FollowUpTaskUpdate,
    db: Session = Depends(get_db),
):
    transcript = db.query(models.TranscriptResponse).filter(models.TranscriptResponse.id == id).first()

    if not transcript:
        raise HTTPException(status_code=404, detail="Transcript not found")

    action = (data.action or "").strip().lower()
    if action not in {"add", "delete", "edit"}:
        raise HTTPException(status_code=400, detail="Invalid action")

    existing = normalize_follow_up_task(transcript.follow_up_task) or ""
    tasks = [t for t in existing.splitlines() if t.strip()]

    if action == "add":
        task = normalize_follow_up_task(data.task)
        if not task:
            raise HTTPException(status_code=400, detail="Task is required")
        tasks.append(task)
    elif action == "delete":
        task = normalize_follow_up_task(data.task)
        if not task:
            raise HTTPException(status_code=400, detail="Task is required")
        tasks = [t for t in tasks if t != task]
    else:
        task = normalize_follow_up_task(data.task)
        new_task = normalize_follow_up_task(data.new_task)
        if not task or not new_task:
            raise HTTPException(status_code=400, detail="Task and new_task are required")
        for index, existing_task in enumerate(tasks):
            if existing_task == task:
                tasks[index] = new_task
                break

    transcript.follow_up_task = "\n".join(tasks) if tasks else None
    db.commit()
    db.refresh(transcript)

    return {
        "message": "Follow up task updated successfully",
        "id": str(transcript.id),
        "follow_up_task": transcript.follow_up_task or "",
    }
