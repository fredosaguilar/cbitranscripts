"""Email a client the file note kept from their call.

This is the written record in a different envelope from the recap text. Where a
recap is a short summary written for a phone screen, this sends the CRM note
itself -- the same words that go on the file -- so what the agency wrote down and
what the client was told are provably the same thing.

That is also the risk. The note is drafted for the file, not for the client, and
a note written for internal use can carry an assessment of the caller, a doubt
about what they said, or an E&O flag raised for the agency's own benefit. None of
that improves for being emailed to them. So nothing here sends on its own: the
note is composed into a message, put in front of an agent, and goes only when
someone has read it and pressed Send.
"""

import hashlib
import logging
import os
import re
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

AGENCY_NAME = os.getenv("AGENCY_NAME", "Columbia Basin Insurance")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
TRANSLATE_MODEL = os.getenv("CLIENT_EMAIL_TRANSLATE_MODEL", os.getenv("TRANSLATE_MODEL", "gpt-4.1-mini"))
TRANSLATE_TIMEOUT = int(os.getenv("CLIENT_EMAIL_TRANSLATE_TIMEOUT", "60"))

# Whisper reports the spoken language as a name or a code depending on the
# model, so both are accepted.
_LANGUAGE_CODES = {
    "en": "en", "eng": "en", "english": "en",
    "es": "es", "spa": "es", "spanish": "es", "espanol": "es", "español": "es", "castellano": "es",
    "pt": "pt", "por": "pt", "portuguese": "pt", "portugues": "pt", "português": "pt",
    "fr": "fr", "fra": "fr", "french": "fr", "francais": "fr", "français": "fr",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "uk": "uk", "ukr": "uk", "ukrainian": "uk",
    "vi": "vi", "vie": "vi", "vietnamese": "vi",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "zh": "zh", "chi": "zh", "chinese": "zh", "mandarin": "zh",
    "tl": "tl", "tgl": "tl", "tagalog": "tl", "filipino": "tl",
    "de": "de", "deu": "de", "german": "de",
    "pa": "pa", "pan": "pa", "punjabi": "pa",
}

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "ru": "Russian", "uk": "Ukrainian", "vi": "Vietnamese", "ar": "Arabic",
    "zh": "Chinese", "tl": "Tagalog", "de": "German", "pa": "Punjabi",
}

# The address a client would reasonably write back to, which is not the mailbox
# the app authenticates as.
CLIENT_EMAIL_FROM = (os.getenv("CLIENT_EMAIL_FROM") or "info@columbiabasininsurance.com").strip()

SUBJECT = os.getenv("CLIENT_NOTE_EMAIL_SUBJECT", "Notes Added to Your File").strip()

AGENCY_PHONE = os.getenv("AGENCY_PHONE", "509-765-8839").strip()
AGENCY_ADDRESS = os.getenv("AGENCY_ADDRESS", "21 D St SW, Suite A, Quincy, WA 98848").strip()

# The licensing line is not decoration. This email carries the file note, and a
# client reading a summary of their own policy should be told in the same breath
# that the policy language is what governs -- the same sentence the agency's
# other mail already closes with.
_LICENCE_LINE = (
    "Licensed in Washington. Coverage descriptions here are summaries only - "
    "your policy language governs."
)


def signature() -> str:
    """The agency's sign-off, matching the one on its other client mail.

    Set CLIENT_EMAIL_SIGNATURE to replace the whole block; otherwise it is built
    from the same agency details the rest of the app already knows, so changing
    the phone number in one place changes it here too.
    """
    override = (os.getenv("CLIENT_EMAIL_SIGNATURE") or "").strip()
    if override:
        return override
    return "\n".join([
        AGENCY_NAME,
        f"Phone: {AGENCY_PHONE}",
        f"Email: {CLIENT_EMAIL_FROM}",
        f"Office: {AGENCY_ADDRESS}",
        "",
        _LICENCE_LINE,
    ])

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

_NO_DATA_VALUES = {
    "", "none", "n/a", "na", "no", "-", "not mentioned", "none noted",
    "not applicable", "no data available", "no data available.", "nothing noted",
    "unknown", "not identified", "not discussed",
}


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NO_DATA_VALUES:
        return None
    return text


def normalize_email(value: Any) -> Optional[str]:
    cleaned = _clean(value)
    if not cleaned:
        return None
    candidate = cleaned.strip().strip("<>").strip()
    return candidate if _EMAIL_PATTERN.match(candidate) else None


def greeting_name(transcript: Any) -> str:
    """What to call the client, falling back to something that is never wrong.

    A carrier label like "Wireless Caller" is not a name, and "Hello Wireless,"
    on a letter about someone's policy reads as carelessly as it sounds.
    """
    name = _clean(getattr(transcript, "client_name", None)) or _clean(getattr(transcript, "from_name", None))
    if not name:
        return "there"
    first = name.replace(",", " ").split()[0]
    if first.lower() in {"wireless", "unknown", "caller", "cell", "toll", "no", "anonymous"}:
        return "there"
    return first.capitalize() if first.islower() or first.isupper() else first


def has_note(transcript: Any) -> bool:
    return bool(_clean(getattr(transcript, "crm_note", None)))


def agent_name(transcript: Any, assigned_name: Optional[str] = None) -> Optional[str]:
    """Who the client spoke to, named the way they would recognise.

    The agent the call is assigned to comes first, since that is the person now
    answerable for it. The RingCentral extension name is the fallback: on a call
    that was never assigned it is still the name of whoever's line it came in
    on, which is who the client actually talked to.
    """
    name = _clean(assigned_name) or _clean(getattr(transcript, "extension_name", None))
    if not name:
        return None
    # Extension names are often "Fred Aguilar - Sales" or "Quincy Office (101)"
    name = re.split(r"\s+[-–|(]", name)[0].strip()
    return name or None


def resolve_language(transcript: Any) -> str:
    """The language the call was spoken in, as a short code. Defaults to English."""
    spoken = (_clean(getattr(transcript, "original_language", None)) or "").lower()
    return _LANGUAGE_CODES.get(spoken, "en")


def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get((code or "en").lower(), (code or "en"))


# One translation per wording, kept for as long as the process lives. The panel
# and the list preview both ask for this text, and on every page load -- paying
# a model call each time to render the same unchanged note would be absurd.
_translations: dict[str, str] = {}

_TRANSLATE_RULES = (
    "Translate this message from an insurance agency to their client into {language}. "
    "It is a record of a phone call, so accuracy matters more than fluency: keep every "
    "name, date, amount, policy number and coverage term exactly as given, translate "
    "nothing into a claim that was not made, and never state that coverage exists where "
    "the original does not. Keep the line breaks and the paragraph structure. Reply with "
    "the translated message only."
)


def translate(text: str, language: str) -> Optional[str]:
    """The message in the client's language, or None if it could not be made.

    Returning None rather than raising is deliberate: a translation that cannot
    be produced should cost the client the second copy, not the email.
    """
    if not text or language == "en" or not OPENAI_API_KEY:
        return None

    key = hashlib.sha256(f"{language}\n{text}".encode("utf-8")).hexdigest()
    if key in _translations:
        return _translations[key]

    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": TRANSLATE_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _TRANSLATE_RULES.format(language=language_name(language))},
                    {"role": "user", "content": text},
                ],
            },
            timeout=TRANSLATE_TIMEOUT,
        )
        response.raise_for_status()
        translated = (response.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("Could not translate a client note email into %s", language)
        return None

    if translated:
        _translations[key] = translated
    return translated or None


def _message(transcript: Any, assigned_name: Optional[str]) -> str:
    """The client-facing wording, without the sign-off."""
    note = _clean(getattr(transcript, "crm_note", None)) or ""
    who = agent_name(transcript, assigned_name)
    # Without a name the sentence has to stand on its own rather than trail off
    # into "your conversation with ." or name the agency twice over.
    conversation = f"your conversation with {who} and the notes" if who else "the notes"
    return (
        f"Hello {greeting_name(transcript)},\n\n"
        f"Here is a summary of {conversation} we added to your file for our record retention:\n\n"
        f"{note}"
    )


def compose(transcript: Any, assigned_name: Optional[str] = None) -> str:
    """The message body, signed off, in both languages when the call was not English.

    The client's own language goes first because that is the copy they will
    read. The English follows it rather than replacing it: the note was written
    in English and that is the wording on the file, so sending only a machine
    translation would mean the agency's record and the client's copy are not the
    same words. Both together, and either one can be checked against the other.
    """
    english = _message(transcript, assigned_name)
    language = resolve_language(transcript)

    # An English call gets one copy, and never asks a model for anything
    translated = translate(english, language) if language != "en" else None

    if not translated:
        return f"{english}\n\n{signature()}\n"

    return (
        f"{translated}\n\n"
        f"{'-' * 40}\n\n"
        f"{english}\n\n"
        f"{signature()}\n"
    )


def resolve_client_email(transcript: Any, agency_zoom_email: Optional[str] = None) -> Optional[str]:
    """The best address on file for this client, if there is one.

    Nothing on the transcript itself holds a client's email, so this is whatever
    the linked Agency Zoom record carries. When there is none the agent types it,
    which is the honest outcome -- guessing an address for a letter about
    somebody's policy is not a thing to do quietly.
    """
    return normalize_email(agency_zoom_email)
