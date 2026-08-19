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

import logging
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

AGENCY_NAME = os.getenv("AGENCY_NAME", "Columbia Basin Insurance")

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


def compose(transcript: Any) -> str:
    """The message body, in the wording the agency asked for, signed off."""
    note = _clean(getattr(transcript, "crm_note", None)) or ""
    return (
        f"Hello {greeting_name(transcript)},\n\n"
        f"Here is a summary of the notes we added to your file for our record retention:\n\n"
        f"{note}\n\n"
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
