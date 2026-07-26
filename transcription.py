"""Transcribe a RingCentral recording server-side.

n8n's download of a recording has repeatedly yielded only the opening
announcement, while the app's own fetch returns the complete file. Doing the
download and the Whisper calls here uses that proven path, and reports the
audio size so a short download is visible rather than silently transcribed.
"""
import logging
import os
import re

import requests
from dotenv import load_dotenv

from ringcentral_utils import fetch_audio_stream

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
TRANSCRIBE_TIMEOUT = int(os.getenv("TRANSCRIBE_TIMEOUT", "600"))
# A complete call is far larger than this; anything smaller is the announcement
MIN_EXPECTED_AUDIO_BYTES = int(os.getenv("MIN_EXPECTED_AUDIO_BYTES", "40000"))

ENGLISH_PROMPT = (
    "Recorded phone call for Columbia Basin Insurance, an insurance agency in Washington State. "
    "Speakers may switch between English and Spanish. Expect insurance vocabulary: policy, premium, "
    "deductible, coverage, liability, comprehensive, collision, endorsement, declarations page, "
    "binder, quote, renewal, claim, adjuster, estimate, rental car, bodily injury, property damage, "
    "uninsured motorist, medical payments, homeowners, umbrella, commercial auto, certificate of "
    "insurance, VIN, effective date, cancellation, non-renewal, down payment, installment. "
    "Carriers: Travelers, Progressive, Safeco, Mutual of Enumclaw, Foremost, Nationwide, Allstate, "
    "State Farm, Kemper, Bristol West."
)
SPANISH_PROMPT = (
    "Llamada telefonica grabada de una agencia de seguros en el estado de Washington. "
    "Vocabulario: poliza, prima, deducible, cobertura, responsabilidad civil, reclamo, ajustador, "
    "aseguranza, cotizacion, vencimiento, pago inicial, endoso, cancelacion, carro de renta, danos, "
    "lesiones corporales, aseguradora, agente, taller."
)


def is_configured() -> bool:
    return bool(OPENAI_API_KEY)


def download_recording(audio_url: str) -> bytes:
    """Read the whole recording into memory using the app's working fetch."""
    response = fetch_audio_stream(audio_url)
    try:
        chunks = [chunk for chunk in response.iter_content(chunk_size=1024 * 64) if chunk]
    finally:
        response.close()
    return b"".join(chunks)


def clean_transcript(text: str) -> str:
    """Collapse the repetition loops Whisper produces on quiet audio."""
    if not text:
        return ""

    parts = re.split(r"(?<=[.?!])\s+", text)
    kept, previous, repeats = [], None, 0
    for part in parts:
        normalized = part.strip().lower()
        if normalized == previous:
            repeats += 1
            if repeats <= 2:
                kept.append(part)
        else:
            previous, repeats = normalized, 1
            kept.append(part)
    cleaned = " ".join(kept)
    return re.sub(r"(.{2,60}?[.!?]\s*)\1{3,}", r"\1\1", cleaned)


def _whisper(endpoint: str, audio: bytes, prompt: str, verbose: bool = False) -> dict:
    files = {"file": ("recording.mp3", audio, "audio/mpeg")}
    data = {"model": WHISPER_MODEL, "prompt": prompt, "temperature": "0"}
    if verbose:
        data["response_format"] = "verbose_json"

    response = requests.post(
        f"{OPENAI_BASE_URL}/audio/{endpoint}",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files=files,
        data=data,
        timeout=TRANSCRIBE_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def transcribe_recording(audio_url: str) -> dict:
    """Return the English translation plus the original-language transcript."""
    if not is_configured():
        raise ValueError("OPENAI_API_KEY is not set, so the app cannot transcribe recordings.")

    audio = download_recording(audio_url)
    audio_bytes = len(audio)
    if not audio_bytes:
        raise ValueError(f"Downloaded no audio from {audio_url}")

    english = clean_transcript(_whisper("translations", audio, ENGLISH_PROMPT).get("text", ""))

    original_payload = _whisper("transcriptions", audio, SPANISH_PROMPT, verbose=True)
    detected = str(original_payload.get("language") or "").lower()
    original_text = clean_transcript(original_payload.get("text", ""))
    spoken_in_english = detected in {"english", "en"}

    result = {
        "text": english,
        "text_original": "" if spoken_in_english else original_text,
        "original_language": detected or "unknown",
        "audio_bytes": audio_bytes,
        "short_download": audio_bytes < MIN_EXPECTED_AUDIO_BYTES,
        "word_count": len(english.split()),
    }
    logger.info(
        "Transcribed %s bytes from %s: %s words%s",
        audio_bytes, audio_url, result["word_count"],
        " (SHORT DOWNLOAD)" if result["short_download"] else "",
    )
    return result
