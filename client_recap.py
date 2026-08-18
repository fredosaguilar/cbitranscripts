"""Write the recap a client receives by text after a call.

The point of the recap is evidence. A client who is told in writing what was
discussed, what they chose, and what is still outstanding either corrects it
straight away or leaves it standing — and a text that stood uncorrected for
months is worth a great deal when a claim is denied and the recollection of the
call turns out to be a matter of dispute.

That only holds if the client can read it, so the recap is written in the
language the call was spoken in. It also only holds if the recap is accurate,
so the wording is drafted from the analysis fields, never from the raw
transcript, and it never states that coverage exists. Confirming coverage in a
text is precisely the exposure this is meant to reduce.

The analysis fields are stored in English regardless of what was spoken, so a
Spanish recap is a translation performed by the model at drafting time. When the
model cannot be reached the draft falls back to English and says so, rather than
sending a client a message in a language nobody chose.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from client_sms import normalize_phone

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
RECAP_MODEL = os.getenv("CLIENT_RECAP_MODEL", "gpt-4.1-mini")
RECAP_TIMEOUT = int(os.getenv("CLIENT_RECAP_TIMEOUT", "60"))

AGENCY_NAME = os.getenv("AGENCY_NAME", "Columbia Basin Insurance")
AGENCY_PHONE = os.getenv("AGENCY_PHONE", "509-765-8839")

# Four GSM-7 segments. Long enough to say what changed, short enough that the
# client actually reads it. Accented languages cost more segments for the same
# characters, which the page's counter shows as it is typed.
RECAP_MAX_CHARS = int(os.getenv("CLIENT_RECAP_MAX_CHARS", "480"))

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
    "mixtec": "mix", "mix": "mix", "triqui": "trq", "trq": "trq",
}

LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "ru": "Russian", "uk": "Ukrainian", "vi": "Vietnamese", "ar": "Arabic",
    "zh": "Chinese", "tl": "Tagalog", "de": "German", "pa": "Punjabi",
    "mix": "Mixtec", "trq": "Triqui",
}

# Every recap goes out in three parts: this opening line, the agent's wording,
# then the opt-out. The opening says what the message is before the client reads
# a word of it — a text that opens with policy details reads like a
# confirmation, and one that opens by naming itself a summary of the call cannot
# be mistaken for one. It carries the legal work too, where it is actually read,
# rather than in small print after the details.
#
# Both fixed lines are written out per language rather than machine-translated
# on the fly. Anything not listed here falls back to English and the page says
# so before the agent sends.
_BUILT_IN_HEADERS = {
    # Plain ASCII on purpose: one typographic dash here would push every English
    # recap into UCS-2 and double what it costs to send.
    "en": (
        "Summary of our recent call. This is not confirmation of coverage. "
        "Reply if anything here is wrong."
    ),
    "es": (
        "Resumen de nuestra llamada reciente. No es una confirmación de cobertura: "
        "responda si algo aquí no es correcto."
    ),
}

# Short by design: it is a legal requirement, not a message, and every character
# it costs comes out of what the client is actually being told.
#
# "STOP" stays in English in every language: carriers match that exact keyword
# to process an opt-out, so translating it would break the opt-out itself.
_BUILT_IN_OPT_OUTS = {
    "en": "Reply STOP to opt out.",
    "es": "Responda STOP para cancelar.",
}

# What to say when a call produced nothing worth reporting back. Any language
# without an entry gets the English one.
_COURTESY = {
    "en": "thanks for your time on the phone today. Call us at {phone} if you have any questions.",
    "es": "gracias por su tiempo hoy. Llámenos al {phone} si tiene alguna pregunta.",
}

# A language written out above is settled: the wording here wins, and no
# environment variable can quietly replace it. These two lines are the part of
# the message that does legal work, so what they say should be visible in the
# code, reviewable in a diff, and identical everywhere the app runs -- not
# something a stale variable in a hosting dashboard can change without anyone
# noticing. A forgotten CLIENT_RECAP_DISCLAIMER_ES doing exactly that is what
# prompted this.
#
# The variables remain for languages NOT written out above, which is the case
# they are actually needed for: adding a language the code does not yet carry,
# without a deploy.
def header_for(language: str | None) -> str:
    """The opening line, which names the message as a summary of the call."""
    code = (language or "en").lower()
    if code in _BUILT_IN_HEADERS:
        return _BUILT_IN_HEADERS[code]
    override = os.getenv(f"CLIENT_RECAP_HEADER_{code.upper()}")
    return _tidy(override) if override else _BUILT_IN_HEADERS["en"]


def opt_out_for(language: str | None) -> str:
    """The closing line, which is the opt-out and nothing else."""
    code = (language or "en").lower()
    if code in _BUILT_IN_OPT_OUTS:
        return _BUILT_IN_OPT_OUTS[code]
    override = os.getenv(f"CLIENT_RECAP_OPT_OUT_{code.upper()}")
    return _tidy(override) if override else _BUILT_IN_OPT_OUTS["en"]


def has_fixed_lines(language: str | None) -> bool:
    """Whether both fixed lines exist in this language rather than English."""
    code = (language or "en").lower()
    return all((
        code in _BUILT_IN_HEADERS or bool(os.getenv(f"CLIENT_RECAP_HEADER_{code.upper()}")),
        code in _BUILT_IN_OPT_OUTS or bool(os.getenv(f"CLIENT_RECAP_OPT_OUT_{code.upper()}")),
    ))

_NO_DATA_VALUES = {
    "", "none", "n/a", "na", "no", "-", "not mentioned", "none noted",
    "not applicable", "no data available", "no data available.", "nothing noted",
    "unknown", "not identified", "not discussed",
}


@dataclass
class Draft:
    body: str
    source: str            # ai | template
    language: str          # the language the body is actually written in
    english_gloss: Optional[str] = None   # for the agent to review, never sent
    warning: Optional[str] = None         # what the agent should know before sending


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _NO_DATA_VALUES:
        return None
    return text


def resolve_language(transcript: Any) -> str:
    """The language the call was spoken in, as a short code. Defaults to English."""
    spoken = (_clean(getattr(transcript, "original_language", None)) or "").lower()
    return _LANGUAGE_CODES.get(spoken, "en")


def language_name(code: str | None) -> str:
    return LANGUAGE_NAMES.get((code or "en").lower(), (code or "en"))


def resolve_client_number(transcript: Any) -> Optional[str]:
    """Work out which number on the call belongs to the client.

    On an inbound call the client is the caller; on an outbound call they are
    the party dialled. Getting this backwards would text the agency's own line,
    so direction decides before any fallback does.
    """
    direction = (_clean(getattr(transcript, "direction", None)) or "").lower()
    if direction.startswith("out"):
        candidates = [
            getattr(transcript, "to_phoneNumber", None),
            getattr(transcript, "client_number", None),
        ]
    else:
        candidates = [
            getattr(transcript, "client_number", None),
            getattr(transcript, "caller_number", None),
        ]
    candidates.append(getattr(transcript, "client_number", None))

    for candidate in candidates:
        normalized = normalize_phone(_clean(candidate))
        if normalized:
            return normalized
    return None


def _first_name(transcript: Any) -> Optional[str]:
    name = _clean(getattr(transcript, "client_name", None)) or _clean(getattr(transcript, "from_name", None))
    if not name:
        return None
    first = name.replace(",", " ").split()[0]
    # "Wireless Caller" and similar carrier labels are not anybody's name
    if first.lower() in {"wireless", "unknown", "caller", "cell", "toll", "no"}:
        return None
    return first.capitalize() if first.islower() or first.isupper() else first


def _facts(transcript: Any) -> list[tuple[str, str]]:
    """The analysis fields worth quoting back, in the order a client reads them."""
    wanted = [
        ("Reason for the call", "reason_for_call"),
        ("What the client wanted", "insured_intent"),
        ("Coverage discussed", "coverage_discussed"),
        ("Options presented", "options_presented"),
        ("What the client chose", "client_selection"),
        ("What the agent recommended", "agent_recommendation"),
        ("Amounts mentioned", "monetary_values"),
        ("Facts about the risk", "material_risk_facts"),
        ("Still outstanding", "missing_information"),
        ("Next steps", "follow_up_task"),
        ("Key points", "key_points"),
    ]
    facts = []
    for label, attribute in wanted:
        value = _clean(getattr(transcript, attribute, None))
        if value:
            facts.append((label, re.sub(r"\s+", " ", value)[:600]))
    return facts


def has_enough_to_recap(transcript: Any) -> bool:
    """Whether the call was analysed enough to describe it back to the client."""
    return any(
        _clean(getattr(transcript, attribute, None))
        for attribute in ("reason_for_call", "coverage_discussed", "client_selection",
                          "agent_recommendation", "follow_up_task", "key_points")
    )


def _budget(language: str = "en") -> int:
    """How much room the agent's own wording has once the fixed lines are paid for."""
    fixed = len(header_for(language)) + len(opt_out_for(language)) + 4
    return max(120, RECAP_MAX_CHARS - fixed)


SYSTEM_PROMPT = """You write the short text message an insurance agency sends a client right after a phone call, confirming in writing what was discussed.

Rules, all of them binding:
- Use ONLY the facts given to you. Never add a detail, a date, an amount, or a coverage that is not there.
- Never state or imply that coverage is in force, bound, added, removed, or effective. Describe what was DISCUSSED, REQUESTED, or SELECTED, not what is covered.
- Never promise a price, a rate, a discount, or an outcome.
- Write to the client as "you", in plain everyday language, past tense, no insurance jargon.
- No greeting, no sign-off, no emoji, no markdown, no bullets with symbols. Short sentences separated by a space or a line break.
- Write the middle of the message only. A line saying this is a summary of the recent call is added above what you write, and an opt-out line is added below it, so never introduce the message or repeat either one — go straight into what was discussed.
- Lead with what changed or what was decided. End with the next step and who owes it, if there is one.
- If the facts are thin, write one honest sentence about what was discussed rather than padding it.

The facts are given to you in English. Write the message in the language named below — the language the client actually spoke on the call — using natural everyday wording a native speaker would use, not a word-for-word translation. Keep names, policy numbers, and amounts exactly as given.

Reply with JSON only, in this shape:
{"message": "<the text message, in the requested language>", "english": "<a plain English rendering of that same message, for the agent to check before sending>"}

When the requested language is English, "english" repeats "message" unchanged."""


def _draft_with_model(transcript: Any, facts: list[tuple[str, str]], language: str) -> Optional[tuple[str, Optional[str]]]:
    """Return (message, english gloss), or None if the model could not be used."""
    if not OPENAI_API_KEY:
        return None

    budget = _budget(language)
    name = _first_name(transcript)
    details = "\n".join(f"{label}: {value}" for label, value in facts)
    user_prompt = (
        f"Agency: {AGENCY_NAME}\n"
        f"Write the message in: {language_name(language)}\n"
        f"Client first name: {name or '(unknown - do not guess, just leave the name out)'}\n"
        f"Hard limit for the message: {budget} characters.\n\n"
        f"Facts from the call:\n{details}"
    )

    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": RECAP_MODEL,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=RECAP_TIMEOUT,
        )
        response.raise_for_status()
        raw = (response.json()["choices"][0]["message"]["content"] or "").strip()
        payload = json.loads(raw)
        message = _tidy(str(payload.get("message") or ""))
        gloss = _tidy(str(payload.get("english") or "")) or None
    except Exception:
        logger.exception("Could not draft a client recap with the model; falling back to the template")
        return None

    if not message:
        return None
    return message, (None if language == "en" else gloss)


# label shown to the client, source field, how important it is to keep, where it reads
_TEMPLATE_LINES = [
    ("Why you called", "reason_for_call", 5, 1),
    ("Coverage discussed", "coverage_discussed", 4, 2),
    ("Options", "options_presented", 6, 3),
    # The decision, the advice given, and what is still owed are the three lines
    # a dispute turns on, so they are the last to be dropped for length.
    ("Your decision", "client_selection", 1, 4),
    ("Our recommendation", "agent_recommendation", 2, 5),
    ("Still needed", "missing_information", 7, 6),
    ("Next", "follow_up_task", 3, 7),
]

# No single line may crowd out the ones that matter more
_TEMPLATE_LINE_MAX = 140


def _draft_from_template(transcript: Any) -> str:
    """A recap written without the model, so drafting never depends on the API.

    Always English: the analysis fields it copies from are English, so there is
    nothing here to write in another language. Deliberately plain — it reads
    like a form, which is the right register for a record. Lines are chosen by
    how much they matter rather than by where they appear, so a long call loses
    "why you called" before it loses the decision the client made.
    """
    # The line above it already says this is a summary of the call, so the body
    # opens straight into the detail rather than announcing itself twice.
    name = _first_name(transcript)
    opening = f"{name}, here is what we went over:" if name else "Here is what we went over:"

    budget = _budget("en") - len(opening)
    chosen: list[tuple[int, str]] = []
    for label, attribute, _importance, position in sorted(_TEMPLATE_LINES, key=lambda row: row[2]):
        value = _clean(getattr(transcript, attribute, None))
        if not value:
            continue
        shortened = _truncate(re.sub(r"\s+", " ", value), _TEMPLATE_LINE_MAX)
        line = f"{label}: {shortened}"
        if len(line) + 1 > budget:
            continue
        budget -= len(line) + 1
        chosen.append((position, line))

    if not chosen:
        return _tidy(f"{opening} thanks for your time today.")

    ordered = [line for _, line in sorted(chosen)]
    return _tidy("\n".join([opening] + ordered))


# Typographic characters cost real money in a text: one em dash pushes the whole
# message out of GSM-7 and into UCS-2, halving how much fits in a segment.
# Letters are left alone — an accented name, or a Spanish message, is worth the
# extra segment; a curly apostrophe is not.
_PUNCTUATION_SWAPS = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
    "•": "-", "·": "-", "−": "-",
}


def _tidy(text: str) -> str:
    """Strip the shapes a model reaches for that do not belong in a text message."""
    if not text:
        return ""
    for character, replacement in _PUNCTUATION_SWAPS.items():
        text = text.replace(character, replacement)
    text = text.strip().strip('"').strip()
    text = re.sub(r"^(here('s| is) (the|your) (draft|message|recap)[:\s-]*)", "", text, flags=re.I)
    text = re.sub(r"^[#>*\-•]+\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    """Cut on a sentence, then a word, so a trimmed recap never ends mid-thought."""
    if len(text) <= limit:
        return text
    window = text[:limit - 3]
    for boundary in (". ", "? ", "! ", ".\n"):
        cut = window.rfind(boundary)
        if cut > limit * 0.5:
            return window[:cut + 1].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(" ,;:-") + "..."


def compose(body: str, language: str = "en") -> str:
    """The message as the client receives it: header, then wording, then opt-out."""
    opening = header_for(language)
    closing = opt_out_for(language)
    trimmed = _truncate(_tidy(body), _budget(language))
    parts = [opening, trimmed, closing] if trimmed else [opening, closing]

    # Blank lines make the three parts read as three parts, but they are paid
    # for like any other character, so a long recap gives them up before it
    # gives up an extra segment.
    joined = "\n\n".join(parts)
    return joined if len(joined) <= RECAP_MAX_CHARS else "\n".join(parts)


def draft_recap(transcript: Any) -> Draft:
    """Write the recap in the language the call was spoken in."""
    language = resolve_language(transcript)
    facts = _facts(transcript)

    # Nothing was analysed, so there is nothing to translate — a fixed courtesy
    # line in the client's language, or English when we have none written.
    if not facts:
        written_in = language if language in _COURTESY else "en"
        courtesy = _COURTESY[written_in].format(phone=AGENCY_PHONE)
        name = _first_name(transcript)
        body = f"{name}, {courtesy}" if name else courtesy[0].upper() + courtesy[1:]
        warning = None if written_in == language else _fallback_warning(language)
        return Draft(body, "template", written_in, None, warning)

    drafted = _draft_with_model(transcript, facts, language)
    if drafted:
        message, gloss = drafted
        return Draft(_truncate(message, _budget(language)), "ai", language, gloss, _language_warning(language))

    return Draft(_truncate(_draft_from_template(transcript), _budget("en")), "template", "en", None,
                 _fallback_warning(language))


def _language_warning(language: str) -> Optional[str]:
    """Flag a message whose fixed lines will not be in the client's language."""
    if language == "en" or has_fixed_lines(language):
        return None
    return (
        f"The recap is written in {language_name(language)}, but the opening and opt-out lines "
        f"are only approved in English — the English ones will be sent. Set "
        f"CLIENT_RECAP_HEADER_{language.upper()} and CLIENT_RECAP_OPT_OUT_{language.upper()} "
        f"to change that."
    )


def _fallback_warning(language: str) -> Optional[str]:
    """Say plainly when a call was not in English but the draft is."""
    if language == "en":
        return None
    return (
        f"This call was in {language_name(language)}, but the draft is in English — "
        f"the model could not be reached. Translate it before sending, or draft again."
    )


def segment_count(text: str) -> int:
    """How many SMS segments a message costs, for the character counter."""
    if not text:
        return 0
    # Anything outside GSM-7 forces the whole message into UCS-2
    unicode_message = bool(re.search(r"[^\x00-\x7F]", text))
    if unicode_message:
        return 1 if len(text) <= 70 else -(-len(text) // 67)
    return 1 if len(text) <= 160 else -(-len(text) // 153)
