from datetime import datetime, timezone
import json
import logging
import os
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
from sqlalchemy import desc
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from database import SessionLocal, engine
import auth
import models
from schemas import TranscriptCreate, UpdateTranscriptRequest, FollowUpTaskUpdate
from send_notification import send_push_notification
from ringcentral_utils import (
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


def _trigger_n8n_webhook_after_delay():
    while True:
        start_time = _get_webhook_start_time_from_cache()
        print('statt_time from cache:', start_time)
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
    context = {"request": request, "users_tokens": users_tokens}
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
    user_token: str = Form(...),
    db: Session = Depends(get_db),
):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    new_user = models.UserToken(user_id=user_id, token=user_token)
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
    user_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.UserToken).filter(models.UserToken.token_id == token_id).first()
    if user:
        user.token = user_token
        db.commit()
    return RedirectResponse(url="/admin/dashboard?updated=1", status_code=303)


@app.get("/admin/transcripts")
# Render the transcript list for admin review.
def list_transcripts(request: Request, db: Session = Depends(get_db)):
    if not get_logged_in_admin(request, db):
        return RedirectResponse(url="/", status_code=303)

    transcripts = (
        db.query(models.TranscriptResponse)
        .order_by(
            models.TranscriptResponse.start_time.desc().nullslast(),
            desc(models.TranscriptResponse.created_at),
        )
        .all()
    )
    users = db.query(models.UserToken.user_id).distinct().all()
    user_ids = sorted([u.user_id for u in users])
    return templates.TemplateResponse(request, "transcripts.html", {
        "request": request,
        "transcripts": transcripts,
        "user_ids": user_ids,
    })


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

    db.add(new_transcript)
    db.commit()
    db.refresh(new_transcript)
    _set_latest_webhook_start_time(new_transcript.start_time, data.start_time)

    data.owner_id = resolved_owner_id
    data.client_number = resolved_client_number
    structured_response = _extract_structured_fields(data)
    user_token = db.query(models.UserToken).filter(models.UserToken.user_id == new_transcript.owner_id).first()
    if user_token:
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
