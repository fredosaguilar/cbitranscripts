"""Send a client a text message.

The agency already runs its phone system on RingCentral, so a recap sent from
the office number arrives from a number the client recognises and can reply to.
Twilio is supported as an alternative for agencies whose RingCentral app has no
SMS scope; nothing else in the app cares which one is in use.

Every send returns a SendResult rather than raising, because the caller records
the outcome either way — a failed send is part of the audit trail, not an
exception to swallow.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv

from ringcentral_utils import (
    RINGCENTRAL_PLATFORM_BASE_URL,
    RINGCENTRAL_REQUEST_TIMEOUT,
    get_ringcentral_access_token,
    ringcentral_api_get,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ringcentral | twilio | auto
CLIENT_SMS_PROVIDER = (os.getenv("CLIENT_SMS_PROVIDER") or "auto").strip().lower()
# When on, messages are logged instead of sent. Worth leaving on until the
# wording has been read on a real handset, since the recipients are real clients.
CLIENT_SMS_DRY_RUN = (os.getenv("CLIENT_SMS_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}

# Every text comes from the agency's main number and no other. A client who gets
# a recap should see the number they already have for the office, and replies --
# which the recap explicitly invites -- have to land somewhere the agency reads,
# not on whichever extension happened to take the call. This is deliberately not
# configurable per agent, per extension, or per call.
AGENCY_MAIN_NUMBER = (
    (os.getenv("RINGCENTRAL_SMS_FROM") or "").strip()
    or (os.getenv("AGENCY_PHONE") or "").strip()
    or "509-765-8839"
)

# Kept under the old name for the settings text and anything else reading it
RINGCENTRAL_SMS_FROM = AGENCY_MAIN_NUMBER

TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_FROM_NUMBER = (os.getenv("TWILIO_FROM_NUMBER") or "").strip()
TWILIO_MESSAGING_SERVICE_SID = (os.getenv("TWILIO_MESSAGING_SERVICE_SID") or "").strip()
TWILIO_TIMEOUT = int(os.getenv("TWILIO_REQUEST_TIMEOUT", "30"))


@dataclass
class SendResult:
    ok: bool
    provider: str
    message_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    # Which number the message actually went out from, recorded on every
    # attempt. "It should have been the office number" is not evidence; the
    # number stored against the message the client received is.
    from_number: Optional[str] = None


def normalize_phone(value: str | None) -> Optional[str]:
    """Return a North American number as E.164, or None if it isn't one.

    Extensions, "Unknown", and the anonymous-caller placeholders RingCentral
    reports all fail this check, which is the point: they must never be dialled
    as if they were a client's mobile.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "anonymous", "restricted", "private", "n/a", "none"}:
        return None

    if text.startswith("+"):
        digits = re.sub(r"\D", "", text)
        # Already E.164 and not North American — pass it through untouched
        if digits and not digits.startswith("1") and 8 <= len(digits) <= 15:
            return f"+{digits}"
    else:
        digits = re.sub(r"\D", "", text)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return f"+1{digits}"


def format_phone_for_display(value: str | None) -> str:
    normalized = normalize_phone(value)
    if not normalized or not normalized.startswith("+1") or len(normalized) != 12:
        return (value or "").strip()
    return f"({normalized[2:5]}) {normalized[5:8]}-{normalized[8:]}"


def _ringcentral_ready() -> bool:
    return bool(RINGCENTRAL_SMS_FROM)


def _twilio_ready() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and (TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID))


def active_provider() -> Optional[str]:
    if CLIENT_SMS_PROVIDER == "ringcentral":
        return "ringcentral" if _ringcentral_ready() else None
    if CLIENT_SMS_PROVIDER == "twilio":
        return "twilio" if _twilio_ready() else None
    if _ringcentral_ready():
        return "ringcentral"
    if _twilio_ready():
        return "twilio"
    return None


def sending_number() -> Optional[str]:
    """The one number every text goes out from, whatever the transcript.

    There is deliberately no per-agent or per-extension sender: a client who
    gets a recap should recognise the number, and one number is also one inbox
    for the replies the recap asks for.
    """
    provider = active_provider()
    if provider == "ringcentral":
        return normalize_phone(RINGCENTRAL_SMS_FROM) or RINGCENTRAL_SMS_FROM or None
    if provider == "twilio":
        if TWILIO_MESSAGING_SERVICE_SID:
            return TWILIO_MESSAGING_SERVICE_SID
        return normalize_phone(TWILIO_FROM_NUMBER) or TWILIO_FROM_NUMBER or None
    return None


def is_configured() -> bool:
    return CLIENT_SMS_DRY_RUN or active_provider() is not None


def describe_configuration() -> str:
    """A sentence the settings page and the API can both show."""
    provider = active_provider()
    if CLIENT_SMS_DRY_RUN:
        via = f" (would send via {provider})" if provider else ""
        return f"Dry run — messages are logged, not delivered{via}."
    if provider == "ringcentral":
        return f"RingCentral, sending from {format_phone_for_display(RINGCENTRAL_SMS_FROM)}."
    if provider == "twilio":
        origin = TWILIO_MESSAGING_SERVICE_SID or format_phone_for_display(TWILIO_FROM_NUMBER)
        return f"Twilio, sending from {origin}."
    return (
        "Not configured. Set RINGCENTRAL_SMS_FROM to the agency's main number, "
        "or set the TWILIO_* variables."
    )


def _error_codes(response) -> set[str]:
    """Every error code RingCentral put in a refusal, top level and nested."""
    try:
        payload = response.json()
    except Exception:
        return set()
    codes = {str(payload.get("errorCode") or "")}
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            codes.add(str(error.get("errorCode") or ""))
    return {code for code in codes if code}


# RingCentral's way of saying the sender is not assigned to the extension the
# app authenticated as. The number is fine; it is being asked for through the
# wrong door.
_WRONG_EXTENSION_CODES = {"MSG-304", "FeatureNotAvailable"}

# The extension that owns the main number, once found. Worth remembering: it
# does not change between messages, and finding it costs an API call.
_sender_extension_id: Optional[str] = None


def _extension_owning_main_number() -> Optional[str]:
    """Which extension the main company number is assigned to, per RingCentral.

    The SMS endpoint is extension-scoped and refuses a sender the extension does
    not own, so a main number that lives on the auto-receptionist cannot be sent
    from as the app's own extension however it is configured. Asking the account
    which extension holds it turns that refusal into a working send.
    """
    global _sender_extension_id
    if _sender_extension_id:
        return _sender_extension_id

    wanted = normalize_phone(AGENCY_MAIN_NUMBER)
    try:
        response = ringcentral_api_get("account/~/phone-number", params={"perPage": 1000})
        if response.status_code >= 400:
            logger.warning(
                "Could not list the account's numbers to find the sender's extension (%s): %s",
                response.status_code, response.text.strip()[:200],
            )
            return None
        for record in response.json().get("records", []):
            if normalize_phone(record.get("phoneNumber")) != wanted:
                continue
            extension_id = ((record.get("extension") or {}).get("id"))
            if extension_id:
                _sender_extension_id = str(extension_id)
                logger.info("The main number %s is on extension %s", AGENCY_MAIN_NUMBER, _sender_extension_id)
                return _sender_extension_id
    except Exception:
        logger.exception("Could not work out which extension owns %s", AGENCY_MAIN_NUMBER)
    return None


def _post_sms(access_token: str, extension: str, to_number: str, body: str):
    url = (
        f"{RINGCENTRAL_PLATFORM_BASE_URL.rstrip('/')}"
        f"/restapi/v1.0/account/~/extension/{extension}/sms"
    )
    return requests.post(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "from": {"phoneNumber": AGENCY_MAIN_NUMBER},
            "to": [{"phoneNumber": to_number}],
            "text": body,
        },
        timeout=RINGCENTRAL_REQUEST_TIMEOUT,
    )


def _send_via_ringcentral(to_number: str, body: str) -> SendResult:
    access_token = get_ringcentral_access_token()
    if not access_token:
        return SendResult(ok=False, provider="ringcentral", error="No RingCentral access token available.")

    # The app's own extension first: that is the path that works when the main
    # number is assigned to it, and it costs no extra lookup.
    response = _post_sms(access_token, "~", to_number, body)

    # Refused because the sender is not on that extension. The number is right --
    # it is the agency's main line -- so find the extension it does belong to and
    # send it from there rather than falling back to some other number.
    sender = format_phone_for_display(AGENCY_MAIN_NUMBER)
    if response.status_code >= 400 and _error_codes(response) & _WRONG_EXTENSION_CODES:
        owning_extension = _extension_owning_main_number()
        if not owning_extension:
            return SendResult(
                ok=False,
                provider="ringcentral",
                error=(
                    f"RingCentral will not send from {sender} as this app's extension, and the extension "
                    f"that owns that number could not be looked up. In RingCentral, either assign {sender} "
                    f"to the extension the app signs in as and turn on its SMS feature, or give the app "
                    f"the ReadAccounts permission so it can find the right extension itself."
                ),
            )

        logger.info("Retrying the text from extension %s, which owns %s", owning_extension, AGENCY_MAIN_NUMBER)
        response = _post_sms(access_token, owning_extension, to_number, body)

        # Refused from the extension that owns the number too, so this is not
        # about which door the request came through: RingCentral is not letting
        # this number text at all. Say that, rather than handing back the same
        # JSON twice with nothing learned from the retry.
        if response.status_code >= 400 and _error_codes(response) & _WRONG_EXTENSION_CODES:
            return SendResult(
                ok=False,
                provider="ringcentral",
                error=(
                    f"RingCentral refused {sender} both as this app's extension and as extension "
                    f"{owning_extension}, which its own records say owns that number. That points at the "
                    f"number rather than the app: check in RingCentral that {sender} still has SMS turned "
                    f"on and that its A2P/10DLC registration is still active."
                ),
            )

    if response.status_code >= 400:
        return SendResult(
            ok=False,
            provider="ringcentral",
            error=f"RingCentral refused the message ({response.status_code}): {response.text.strip()[:500]}",
        )

    payload = response.json()
    status = str(payload.get("messageStatus") or "")
    # Queued and Sent both mean RingCentral accepted it; SendingFailed does not.
    if status.lower() in {"sendingfailed", "deliveryfailed"}:
        detail = payload.get("to", [{}])[0].get("messageStatusDetail") if payload.get("to") else None
        return SendResult(
            ok=False,
            provider="ringcentral",
            message_id=str(payload.get("id") or "") or None,
            status=status,
            error=f"RingCentral could not deliver the message: {detail or status}",
        )

    return SendResult(ok=True, provider="ringcentral", message_id=str(payload.get("id") or "") or None, status=status)


def _send_via_twilio(to_number: str, body: str) -> SendResult:
    data = {"To": to_number, "Body": body}
    if TWILIO_MESSAGING_SERVICE_SID:
        data["MessagingServiceSid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        data["From"] = TWILIO_FROM_NUMBER

    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data=data,
        timeout=TWILIO_TIMEOUT,
    )
    if response.status_code >= 400:
        return SendResult(
            ok=False,
            provider="twilio",
            error=f"Twilio refused the message ({response.status_code}): {response.text.strip()[:500]}",
        )

    payload = response.json()
    return SendResult(
        ok=True,
        provider="twilio",
        message_id=payload.get("sid"),
        status=payload.get("status"),
    )


def send_sms(to_number: str, body: str) -> SendResult:
    """Text one client. Never raises; the outcome is the return value."""
    normalized_to = normalize_phone(to_number)
    if not normalized_to:
        return SendResult(ok=False, provider="none", error=f"{to_number!r} is not a textable phone number.")
    if not (body or "").strip():
        return SendResult(ok=False, provider="none", error="Refusing to send an empty message.")

    if CLIENT_SMS_DRY_RUN:
        logger.info("DRY RUN — would text %s: %s", normalized_to, body)
        return SendResult(ok=True, provider="dry-run", message_id="dry-run", status="DryRun",
                          from_number=sending_number())

    provider = active_provider()
    if provider is None:
        return SendResult(ok=False, provider="none", error="No SMS provider is configured.")

    try:
        if provider == "ringcentral":
            result = _send_via_ringcentral(normalized_to, body)
        else:
            result = _send_via_twilio(normalized_to, body)
    except Exception as exc:
        logger.exception("Texting %s failed", normalized_to)
        return SendResult(ok=False, provider=provider, error=f"{type(exc).__name__}: {exc}")

    result.from_number = result.from_number or sending_number()

    if result.ok:
        logger.info("Texted %s from %s via %s (message %s)",
                    normalized_to, result.from_number, result.provider, result.message_id)
    else:
        logger.warning("Could not text %s via %s: %s", normalized_to, result.provider, result.error)
    return result
