"""Write the recap a client receives by text after a call.

The point of the recap is evidence. A client who is told in writing what was
discussed, what they chose, and what is still outstanding either corrects it
straight away or leaves it standing — and a text that stood uncorrected for
months is worth a great deal when a claim is denied and the recollection of the
call turns out to be a matter of dispute.

That only holds if the recap is accurate, so the wording is drafted from the
analysis fields, never from the raw transcript, and it never states that
coverage exists. Confirming coverage in a text is precisely the exposure this
is meant to reduce.
"""

import logging
import os
import re
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
# client actually reads it.
RECAP_MAX_CHARS = int(os.getenv("CLIENT_RECAP_MAX_CHARS", "480"))
# Room the closing line needs, reserved out of the budget given to the model.
DISCLAIMER = os.getenv(
    "CLIENT_RECAP_DISCLAIMER",
    "This is a summary of our conversation, not confirmation of coverage. "
    "Reply if anything here is wrong. Reply STOP to opt out.",
)

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


def resolve_client_number(transcript: Any) -> Optional[str]:
    """Work out which number on the call belongs to the client.

    On an inbound call the client is the caller; on an outbound call they are
    the party dialled. Getting this backwards would text the agency's own line,
    so direction decides before any fallback does.
    """
    direction = (_clean(getattr(transcript, "direction", None)) or "").lower()
    candidates: list[Any] = []
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


def _budget() -> int:
    return max(120, RECAP_MAX_CHARS - len(DISCLAIMER) - 2)


SYSTEM_PROMPT = """You write the short text message an insurance agency sends a client right after a phone call, confirming in writing what was discussed.

Rules, all of them binding:
- Use ONLY the facts given to you. Never add a detail, a date, an amount, or a coverage that is not there.
- Never state or imply that coverage is in force, bound, added, removed, or effective. Describe what was DISCUSSED, REQUESTED, or SELECTED, not what is covered.
- Never promise a price, a rate, a discount, or an outcome.
- Write to the client as "you", in plain English, past tense, no insurance jargon.
- No greeting, no sign-off, no emoji, no markdown, no bullets with symbols. Short sentences separated by a space or a line break.
- Lead with what changed or what was decided. End with the next step and who owes it, if there is one.
- If the facts are thin, write one honest sentence about what was discussed rather than padding it.

Reply with the message text and nothing else."""


def _draft_with_model(transcript: Any, facts: list[tuple[str, str]]) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    budget = _budget()
    name = _first_name(transcript)
    details = "\n".join(f"{label}: {value}" for label, value in facts)
    user_prompt = (
        f"Agency: {AGENCY_NAME}\n"
        f"Client first name: {name or '(unknown — do not guess, just leave the name out)'}\n"
        f"Hard limit: {budget} characters.\n\n"
        f"Facts from the call:\n{details}"
    )

    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": RECAP_MODEL,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=RECAP_TIMEOUT,
        )
        response.raise_for_status()
        text = (response.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.exception("Could not draft a client recap with the model; falling back to the template")
        return None

    return _tidy(text) or None


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


def _draft_from_template(transcript: Any, facts: list[tuple[str, str]]) -> str:
    """A recap written without the model, so drafting never depends on the API.

    Deliberately plain — it reads like a form, which is the right register for a
    record. Lines are chosen by how much they matter rather than by where they
    appear, so a long call loses "why you called" before it loses the decision
    the client made.
    """
    name = _first_name(transcript)
    opening = f"{name}, a recap of our call:" if name else "A recap of our call:"

    budget = _budget() - len(opening)
    chosen: list[tuple[int, str]] = []
    for label, attribute, importance, position in sorted(_TEMPLATE_LINES, key=lambda row: row[2]):
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
# Letters are left alone — an accented name is worth the extra segment, a curly
# apostrophe is not.
_PUNCTUATION_SWAPS = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
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


def compose(body: str) -> str:
    """Put the agent's wording and the standing disclaimer together."""
    trimmed = _truncate(_tidy(body), _budget())
    if not trimmed:
        return DISCLAIMER
    separator = "\n\n" if len(trimmed) + len(DISCLAIMER) + 2 <= RECAP_MAX_CHARS else "\n"
    return f"{trimmed}{separator}{DISCLAIMER}"


def draft_recap(transcript: Any) -> tuple[str, str]:
    """Return the draft body (without the disclaimer) and how it was written."""
    facts = _facts(transcript)
    if not facts:
        name = _first_name(transcript)
        opening = f"{name}, thanks for your time on the phone today." if name else "Thanks for your time on the phone today."
        return f"{opening} Call us at {AGENCY_PHONE} if you have any questions.", "template"

    drafted = _draft_with_model(transcript, facts)
    if drafted:
        return _truncate(drafted, _budget()), "ai"
    return _truncate(_draft_from_template(transcript, facts), _budget()), "template"


def segment_count(text: str) -> int:
    """How many SMS segments a message costs, for the character counter."""
    if not text:
        return 0
    # Anything outside GSM-7 forces the whole message into UCS-2
    unicode_message = bool(re.search(r"[^\x00-\x7F]", text))
    if unicode_message:
        return 1 if len(text) <= 70 else -(-len(text) // 67)
    return 1 if len(text) <= 160 else -(-len(text) // 153)
