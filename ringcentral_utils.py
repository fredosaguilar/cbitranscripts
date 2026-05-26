import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RINGCENTRAL_MEDIA_BASE_URL = os.getenv("RINGCENTRAL_MEDIA_BASE_URL", "https://media.ringcentral.com/restapi/v1.0")
RINGCENTRAL_PLATFORM_BASE_URL = os.getenv("RINGCENTRAL_PLATFORM_BASE_URL", "https://platform.ringcentral.com")
RINGCENTRAL_TOKEN_URL = os.getenv(
    "RINGCENTRAL_TOKEN_URL",
    f"{RINGCENTRAL_PLATFORM_BASE_URL.rstrip('/')}/restapi/oauth/token",
)
RINGCENTRAL_ACCOUNT_ID = os.getenv("RINGCENTRAL_ACCOUNT_ID") or os.getenv("RC_ACCOUNT_ID")
RINGCENTRAL_STATIC_ACCESS_TOKEN = (
    os.getenv("RINGCENTRAL_ACCESS_TOKEN")
    or os.getenv("RINGCENTRAL_BEARER_TOKEN")
    or os.getenv("RC_ACCESS_TOKEN")
)
RINGCENTRAL_COOKIE = os.getenv("RINGCENTRAL_COOKIE") or os.getenv("RC_COOKIE")
RINGCENTRAL_BASIC_AUTH = os.getenv("RINGCENTRAL_BASIC_AUTH") or os.getenv("RC_BASIC_AUTH")
RINGCENTRAL_USERNAME = os.getenv("RINGCENTRAL_USERNAME")
RINGCENTRAL_EXTENSION = os.getenv("RINGCENTRAL_EXTENSION", "")
RINGCENTRAL_PASSWORD = os.getenv("RINGCENTRAL_PASSWORD")
RINGCENTRAL_JWT_ASSERTION = os.getenv("RINGCENTRAL_JWT_ASSERTION")
RINGCENTRAL_GRANT_TYPE = os.getenv(
    "RINGCENTRAL_GRANT_TYPE",
    "urn:ietf:params:oauth:grant-type:jwt-bearer",
)
RINGCENTRAL_REQUEST_TIMEOUT = int(os.getenv("RINGCENTRAL_REQUEST_TIMEOUT", "60"))
RINGCENTRAL_TOKEN_REFRESH_BUFFER_SECONDS = int(
    os.getenv("RINGCENTRAL_TOKEN_REFRESH_BUFFER_SECONDS", "60")
)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_AUDIO_CACHE_DIR = os.getenv("LOCAL_AUDIO_CACHE_DIR") or os.path.join(BASE_DIR, "static", "audio_cache")

_token_lock = threading.Lock()
_cached_access_token: Optional[str] = None
_cached_access_token_expiry: Optional[datetime] = None


def _clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned if cleaned else None


def _build_basic_auth_header(value: str | None) -> Optional[str]:
    cleaned_value = _clean_string(value)
    if not cleaned_value:
        return None
    if cleaned_value.lower().startswith("basic "):
        return cleaned_value
    return f"Basic {cleaned_value}"


def _normalize_relative_path(path: str | None) -> Optional[str]:
    cleaned_path = _clean_string(path)
    if not cleaned_path:
        return None
    normalized_path = os.path.normpath(cleaned_path)
    if os.path.isabs(normalized_path):
        return normalized_path
    return normalized_path


def resolve_local_audio_absolute_path(local_audio_path: str | None) -> Optional[str]:
    normalized_path = _normalize_relative_path(local_audio_path)
    if not normalized_path:
        return None
    if os.path.isabs(normalized_path):
        return normalized_path
    return os.path.join(BASE_DIR, normalized_path)


def get_existing_local_audio_path(local_audio_path: str | None) -> Optional[str]:
    absolute_path = resolve_local_audio_absolute_path(local_audio_path)
    if absolute_path and os.path.exists(absolute_path):
        return absolute_path
    return None


def delete_local_audio_file(local_audio_path: str | None) -> bool:
    absolute_path = resolve_local_audio_absolute_path(local_audio_path)
    if not absolute_path or not os.path.exists(absolute_path):
        return False

    cache_root = os.path.abspath(LOCAL_AUDIO_CACHE_DIR)
    target_path = os.path.abspath(absolute_path)
    try:
        if os.path.commonpath([cache_root, target_path]) != cache_root:
            logger.warning("Skipping delete for non-cache audio path: %s", target_path)
            return False
    except ValueError:
        logger.warning("Skipping delete for invalid audio path: %s", target_path)
        return False

    os.remove(target_path)
    logger.info("Deleted cached audio file: %s", target_path)
    return True


def _sanitize_file_stem(value: str | None, fallback: str) -> str:
    cleaned_value = _clean_string(value) or fallback
    safe_value = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in cleaned_value)
    safe_value = safe_value.strip("._")
    return safe_value or fallback


def _extension_from_content_type(content_type: str | None) -> str:
    normalized = (_clean_string(content_type) or "").split(";")[0].strip().lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/ogg": ".ogg",
    }.get(normalized, "")


def _extension_from_url(url: str | None) -> str:
    cleaned_url = _clean_string(url)
    if not cleaned_url:
        return ""
    parsed_url = urlparse(cleaned_url)
    _, extension = os.path.splitext(parsed_url.path)
    extension = extension.lower()
    if extension in {".mp3", ".wav", ".m4a", ".mp4", ".ogg"}:
        return ".m4a" if extension == ".mp4" else extension
    return ""


def is_ringcentral_url(url: str | None) -> bool:
    cleaned_url = _clean_string(url)
    if not cleaned_url:
        return False
    return "ringcentral" in cleaned_url.lower()


def build_ringcentral_recording_url(recording_id: str | None) -> str | None:
    normalized_recording_id = _clean_string(recording_id)
    normalized_account_id = _clean_string(RINGCENTRAL_ACCOUNT_ID)
    if not normalized_recording_id or not normalized_account_id:
        return None
    base_url = RINGCENTRAL_MEDIA_BASE_URL.rstrip("/")
    return f"{base_url}/account/{normalized_account_id}/recording/{normalized_recording_id}/content"


def resolve_transcript_audio_url(transcript: Any) -> str | None:
    file_link = _clean_string(getattr(transcript, "file_link", None))
    if file_link:
        return file_link
    return build_ringcentral_recording_url(getattr(transcript, "recordingID", None))


def guess_audio_content_type(url: str | None) -> str:
    cleaned_url = (_clean_string(url) or "").lower()
    if ".mp3" in cleaned_url:
        return "audio/mpeg"
    if ".wav" in cleaned_url:
        return "audio/wav"
    if ".m4a" in cleaned_url or ".mp4" in cleaned_url:
        return "audio/mp4"
    if ".ogg" in cleaned_url:
        return "audio/ogg"
    return "audio/mpeg"


def _is_cached_token_valid() -> bool:
    if not _cached_access_token or not _cached_access_token_expiry:
        return False
    refresh_time = _cached_access_token_expiry - timedelta(seconds=RINGCENTRAL_TOKEN_REFRESH_BUFFER_SECONDS)
    return datetime.now(timezone.utc) < refresh_time


def _can_refresh_ringcentral_token() -> bool:
    return bool(_clean_string(RINGCENTRAL_BASIC_AUTH) and _clean_string(RINGCENTRAL_JWT_ASSERTION))


def _cache_access_token(access_token: str, expires_in: Any = None) -> str:
    global _cached_access_token, _cached_access_token_expiry

    _cached_access_token = access_token
    expiry_seconds = None
    try:
        if expires_in is not None:
            expiry_seconds = int(expires_in)
    except (TypeError, ValueError):
        expiry_seconds = None

    if expiry_seconds:
        _cached_access_token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
    else:
        _cached_access_token_expiry = None

    return access_token


def _fetch_ringcentral_access_token() -> str:
    if not _can_refresh_ringcentral_token():
        raise ValueError(
            "RingCentral token refresh is not configured. Set RINGCENTRAL_BASIC_AUTH, RINGCENTRAL_JWT_ASSERTION, and RINGCENTRAL_ACCOUNT_ID."
        )

    payload = {
        "grant_type": RINGCENTRAL_GRANT_TYPE,
        "assertion": RINGCENTRAL_JWT_ASSERTION,
        "extension": RINGCENTRAL_EXTENSION,
    }
    username = _clean_string(RINGCENTRAL_USERNAME)
    password = _clean_string(RINGCENTRAL_PASSWORD)
    if username:
        payload["username"] = username
    if password:
        payload["password"] = password

    basic_auth_header = _build_basic_auth_header(RINGCENTRAL_BASIC_AUTH)
    if not basic_auth_header:
        raise ValueError("RingCentral basic auth header is missing. Set RINGCENTRAL_BASIC_AUTH in the environment.")

    headers = {
        "Authorization": basic_auth_header,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    cookie = _clean_string(RINGCENTRAL_COOKIE)
    if cookie:
        headers["Cookie"] = cookie

    response = requests.post(
        RINGCENTRAL_TOKEN_URL,
        headers=headers,
        data=payload,
        timeout=RINGCENTRAL_REQUEST_TIMEOUT,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        response_text = response.text.strip()
        detail = f"RingCentral token refresh failed with status {response.status_code}."
        if response_text:
            detail = f"{detail} Response: {response_text}"
        raise ValueError(detail) from exc

    response_data = response.json()
    access_token = _clean_string(response_data.get("access_token"))
    if not access_token:
        raise ValueError(f"RingCentral token response did not include access_token: {response_data}")

    logger.info("Fetched a fresh RingCentral access token")
    return _cache_access_token(access_token, response_data.get("expires_in"))


def get_ringcentral_access_token(force_refresh: bool = False) -> str | None:
    static_token = _clean_string(RINGCENTRAL_STATIC_ACCESS_TOKEN)

    with _token_lock:
        if not force_refresh and _is_cached_token_valid():
            return _cached_access_token

        if _can_refresh_ringcentral_token():
            return _fetch_ringcentral_access_token()

        if static_token:
            return static_token

    return None


def _build_audio_request_headers(url: str, byte_range: str | None = None, force_refresh: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if byte_range:
        headers["Range"] = byte_range

    if is_ringcentral_url(url):
        access_token = get_ringcentral_access_token(force_refresh=force_refresh)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        cookie = _clean_string(RINGCENTRAL_COOKIE)
        if cookie:
            headers["Cookie"] = cookie

    return headers


def fetch_audio_stream(audio_url: str, byte_range: str | None = None) -> requests.Response:
    request_headers = _build_audio_request_headers(audio_url, byte_range=byte_range)
    response = requests.get(
        audio_url,
        headers=request_headers,
        stream=True,
        timeout=RINGCENTRAL_REQUEST_TIMEOUT,
    )

    if response.status_code in {401, 403} and is_ringcentral_url(audio_url) and _can_refresh_ringcentral_token():
        response.close()
        refreshed_headers = _build_audio_request_headers(audio_url, byte_range=byte_range, force_refresh=True)
        response = requests.get(
            audio_url,
            headers=refreshed_headers,
            stream=True,
            timeout=RINGCENTRAL_REQUEST_TIMEOUT,
        )

    response.raise_for_status()
    return response


def cache_audio_file(transcript: Any, audio_url: str) -> tuple[str, str]:
    os.makedirs(LOCAL_AUDIO_CACHE_DIR, exist_ok=True)

    response = fetch_audio_stream(audio_url)
    try:
        content_type = _clean_string(response.headers.get("content-type")) or guess_audio_content_type(audio_url)
        extension = _extension_from_content_type(content_type) or _extension_from_url(audio_url) or ".mp3"
        file_stem = _sanitize_file_stem(
            getattr(transcript, "recordingID", None) or getattr(transcript, "id", None),
            "transcript_audio",
        )
        filename = f"{file_stem}{extension}"
        absolute_path = os.path.join(LOCAL_AUDIO_CACHE_DIR, filename)
        relative_path = os.path.relpath(absolute_path, BASE_DIR)

        if os.path.exists(absolute_path):
            return relative_path, content_type

        temp_path = f"{absolute_path}.part"
        with open(temp_path, "wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if chunk:
                    file_handle.write(chunk)
        os.replace(temp_path, absolute_path)
        logger.info("Cached audio locally for transcript %s at %s", getattr(transcript, "id", None), relative_path)
        return relative_path, content_type
    finally:
        response.close()
