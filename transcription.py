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

# Whisper echoes a long prompt back verbatim when it gives up on a segment, so
# these stay short: just enough to bias the vocabulary, not enough to become the
# transcript.
ENGLISH_PROMPT = "Insurance call: policy, premium, deductible, coverage, claim, adjuster, endorsement, quote."
SPANISH_PROMPT = "Llamada de seguros: poliza, prima, deducible, cobertura, reclamo, ajustador, endoso, cotizacion."

# Whisper stops early on long low-bitrate phone recordings, returning only the
# opening announcement. Sending the call in pieces makes each piece a fresh
# decode, so one bad segment cannot swallow the whole conversation.
CHUNK_SECONDS = int(os.getenv("TRANSCRIBE_CHUNK_SECONDS", "60"))

_MP3_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_MP3_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_MP3_RATES = {0: [11025, 12000, 8000], 2: [22050, 24000, 16000], 3: [44100, 48000, 32000]}


def split_mp3(audio: bytes, chunk_seconds: int = CHUNK_SECONDS) -> list[bytes]:
    """Cut an MP3 on frame boundaries, without re-encoding.

    Returns the whole file as a single chunk when the frames cannot be parsed,
    so an unexpected format still gets transcribed rather than dropped.
    """
    position = 0
    if audio[:3] == b"ID3":
        size = ((audio[6] & 0x7F) << 21) | ((audio[7] & 0x7F) << 14) | ((audio[8] & 0x7F) << 7) | (audio[9] & 0x7F)
        position = 10 + size

    chunks: list[bytes] = []
    chunk_start = position
    elapsed = 0.0

    while position < len(audio) - 4:
        if audio[position] != 0xFF or (audio[position + 1] & 0xE0) != 0xE0:
            position += 1
            continue

        version = (audio[position + 1] >> 3) & 0x03
        bitrate_index = (audio[position + 2] >> 4) & 0x0F
        rate_index = (audio[position + 2] >> 2) & 0x03
        padding = (audio[position + 2] >> 1) & 0x01

        rates = _MP3_RATES.get(version)
        if rates is None or rate_index > 2:
            position += 1
            continue
        sample_rate = rates[rate_index]
        table = _MP3_BITRATES_V1_L3 if version == 3 else _MP3_BITRATES_V2_L3
        bitrate = table[bitrate_index] * 1000
        if not bitrate or not sample_rate:
            position += 1
            continue

        samples_per_frame = 1152 if version == 3 else 576
        frame_length = int(samples_per_frame / 8 * bitrate / sample_rate) + padding
        if frame_length < 4:
            position += 1
            continue

        position += frame_length
        elapsed += samples_per_frame / sample_rate
        if elapsed >= chunk_seconds:
            chunks.append(audio[chunk_start:position])
            chunk_start = position
            elapsed = 0.0

    if chunk_start < len(audio):
        tail = audio[chunk_start:]
        if len(tail) > 512:
            chunks.append(tail)

    return chunks or [audio]


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

    chunks = split_mp3(audio)
    english_parts: list[str] = []
    original_parts: list[str] = []
    detected = ""

    for index, chunk in enumerate(chunks, start=1):
        try:
            english_parts.append(_whisper("translations", chunk, ENGLISH_PROMPT).get("text", "").strip())
        except Exception:
            # One unreadable minute must not cost the rest of the call
            logger.exception("Translation failed for chunk %s/%s of %s", index, len(chunks), audio_url)

        try:
            payload = _whisper("transcriptions", chunk, SPANISH_PROMPT, verbose=True)
            original_parts.append(str(payload.get("text", "")).strip())
            if not detected:
                detected = str(payload.get("language") or "").lower()
        except Exception:
            logger.exception("Transcription failed for chunk %s/%s of %s", index, len(chunks), audio_url)

    english = clean_transcript(" ".join(part for part in english_parts if part))
    original_text = clean_transcript(" ".join(part for part in original_parts if part))
    spoken_in_english = detected in {"english", "en"}

    result = {
        "text": english,
        "text_original": "" if spoken_in_english else original_text,
        "original_language": detected or "unknown",
        "audio_bytes": audio_bytes,
        "chunks": len(chunks),
        "short_download": audio_bytes < MIN_EXPECTED_AUDIO_BYTES,
        "word_count": len(english.split()),
    }
    logger.info(
        "Transcribed %s bytes from %s in %s chunk(s): %s words%s",
        audio_bytes, audio_url, len(chunks), result["word_count"],
        " (SHORT DOWNLOAD)" if result["short_download"] else "",
    )
    return result
