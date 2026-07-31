"""Transcribe a RingCentral recording server-side.

n8n's download of a recording has repeatedly yielded only the opening
announcement, while the app's own fetch returns the complete file. Doing the
download and the Whisper calls here uses that proven path, and reports the
audio size so a short download is visible rather than silently transcribed.
"""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

from ringcentral_utils import fetch_audio_stream

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
# Used to put a Spanish call into English. Translating text costs a fraction of
# sending the audio through Whisper a second time.
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "gpt-4.1-mini")
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

# One chunk at a time made a long call take minutes, long enough for the caller
# to give up before the transcript existed, so long calls never appeared at all.
# The chunks are independent, so they are sent together.
MAX_PARALLEL_CHUNKS = int(os.getenv("TRANSCRIBE_PARALLEL_CHUNKS", "6"))
WHISPER_RETRIES = int(os.getenv("TRANSCRIBE_RETRIES", "2"))

_MP3_BITRATES_V2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
_MP3_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_MP3_RATES = {0: [11025, 12000, 8000], 2: [22050, 24000, 16000], 3: [44100, 48000, 32000]}



def _iter_mp3_frames(audio: bytes, with_offsets: bool = False):
    """Yield each MP3 frame, skipping any ID3 header."""
    position = 0
    if audio[:3] == b"ID3":
        size = ((audio[6] & 0x7F) << 21) | ((audio[7] & 0x7F) << 14) | ((audio[8] & 0x7F) << 7) | (audio[9] & 0x7F)
        position = 10 + size

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

        if with_offsets:
            yield position, samples_per_frame, sample_rate, frame_length
        else:
            yield position, samples_per_frame, sample_rate
        position += frame_length


def mp3_duration_seconds(audio: bytes) -> float:
    """Duration from the MP3 frame headers, for spotting a short download."""
    total = 0.0
    for _, samples_per_frame, sample_rate in _iter_mp3_frames(audio):
        total += samples_per_frame / sample_rate
    return total


def split_mp3(audio: bytes, chunk_seconds: int = CHUNK_SECONDS) -> list[bytes]:
    """Cut an MP3 on frame boundaries, without re-encoding.

    Returns the whole file as a single chunk when the frames cannot be parsed,
    so an unexpected format still gets transcribed rather than dropped.
    """
    chunks: list[bytes] = []
    chunk_start = None
    elapsed = 0.0

    for offset, samples_per_frame, sample_rate, frame_length in _iter_mp3_frames(audio, with_offsets=True):
        if chunk_start is None:
            chunk_start = offset
        elapsed += samples_per_frame / sample_rate
        if elapsed >= chunk_seconds:
            chunks.append(audio[chunk_start:offset + frame_length])
            chunk_start = offset + frame_length
            elapsed = 0.0

    if chunk_start is not None and chunk_start < len(audio):
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

    # Sending chunks together makes a rate-limit reply likely, and a dropped
    # chunk is a missing minute of the call, so a refusal is worth retrying.
    for attempt in range(WHISPER_RETRIES + 1):
        response = requests.post(
            f"{OPENAI_BASE_URL}/audio/{endpoint}",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
            timeout=TRANSCRIBE_TIMEOUT,
        )
        if response.status_code in (429, 500, 502, 503, 504) and attempt < WHISPER_RETRIES:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response.json()

    response.raise_for_status()
    return response.json()


def _transcribe_chunk(chunk: bytes, index: int, total: int) -> tuple[str, str]:
    """Transcribe one chunk as spoken, returning its text and its language."""
    try:
        payload = _whisper("transcriptions", chunk, SPANISH_PROMPT, verbose=True)
        return str(payload.get("text", "")).strip(), str(payload.get("language") or "").lower()
    except Exception:
        # One unreadable minute must not cost the rest of the call
        logger.exception("Transcription failed for chunk %s/%s", index, total)
        return "", ""


def _translate_text(text: str) -> str:
    """Translate a finished transcript into English with a text model.

    Whisper charges by the minute of audio, so translating by sending the call
    through it a second time costs as much again as transcribing it. Sending
    the text instead costs a fraction of a cent, and is the same work.
    """
    if not text:
        return ""

    response = requests.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": TRANSLATE_MODEL,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Translate the insurance phone call transcript into English. Keep every "
                        "detail, name, number, and amount exactly as spoken. Do not summarise, "
                        "explain, or add anything. Reply with the translation only."
                    ),
                },
                {"role": "user", "content": text},
            ],
        },
        timeout=TRANSCRIBE_TIMEOUT,
    )
    response.raise_for_status()
    return (response.json()["choices"][0]["message"]["content"] or "").strip()


def _translate_chunks_with_whisper(chunks: list[bytes]) -> str:
    """Fall back to Whisper's own translation if the text model is unavailable."""
    parts: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, MAX_PARALLEL_CHUNKS)) as pool:
        for text in pool.map(lambda c: _safe_whisper_translation(c), chunks):
            parts.append(text)
    return " ".join(part for part in parts if part)


def _safe_whisper_translation(chunk: bytes) -> str:
    try:
        return _whisper("translations", chunk, ENGLISH_PROMPT).get("text", "").strip()
    except Exception:
        logger.exception("Whisper translation failed for a chunk")
        return ""


def load_recording(audio_url: str) -> tuple[bytes, float]:
    """Fetch a recording and measure it, before deciding whether to pay to read it."""
    audio = download_recording(audio_url)
    return audio, mp3_duration_seconds(audio)


def transcribe_recording(audio_url: str) -> dict:
    audio, audio_seconds = load_recording(audio_url)
    return transcribe_audio(audio, audio_seconds, audio_url)


def transcribe_audio(audio: bytes, audio_seconds: float, audio_url: str = "") -> dict:
    """Return the English transcript plus the original-language transcript."""
    if not is_configured():
        raise ValueError("OPENAI_API_KEY is not set, so the app cannot transcribe recordings.")

    audio_bytes = len(audio)
    if not audio_bytes:
        raise ValueError(f"Downloaded no audio from {audio_url}")

    started = time.monotonic()
    chunks = split_mp3(audio)
    total = len(chunks)

    # Every chunk is read once, as spoken. The English version is made from that
    # text afterwards, so the audio is never sent to Whisper twice.
    transcribed: list[tuple[str, str]] = []
    if total == 1:
        transcribed.append(_transcribe_chunk(chunks[0], 1, total))
    else:
        with ThreadPoolExecutor(max_workers=max(1, MAX_PARALLEL_CHUNKS)) as pool:
            transcribed = list(pool.map(
                lambda item: _transcribe_chunk(item[1], item[0], total),
                list(enumerate(chunks, start=1)),
            ))

    detected = next((language for _, language in transcribed if language), "")
    spoken_in_english = detected in {"english", "en"}
    original_text = clean_transcript(" ".join(text for text, _ in transcribed if text))

    if spoken_in_english:
        english = original_text
    else:
        try:
            english = clean_transcript(_translate_text(original_text))
        except Exception:
            logger.exception("Text translation failed for %s; falling back to Whisper", audio_url)
            english = clean_transcript(_translate_chunks_with_whisper(chunks))

    result = {
        "audio_seconds": round(audio_seconds, 1),
        "text": english,
        "text_original": "" if spoken_in_english else original_text,
        "original_language": detected or "unknown",
        "audio_bytes": audio_bytes,
        "chunks": len(chunks),
        "short_download": audio_bytes < MIN_EXPECTED_AUDIO_BYTES,
        "word_count": len(english.split()),
    }
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    logger.info(
        "Transcribed %s bytes (%.0fs of audio) from %s in %s chunk(s) in %ss: %s words%s",
        audio_bytes, audio_seconds, audio_url, len(chunks), result["elapsed_seconds"], result["word_count"],
        " (SHORT DOWNLOAD)" if result["short_download"] else "",
    )
    return result
