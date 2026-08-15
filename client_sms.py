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
    get_ringcentral_scopes,
    ringcentral_api_get,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ringcentral | twilio | auto
CLIENT_SMS_PROVIDER = (os.getenv("CLIENT_SMS_PROVIDER") or "auto").strip().lower()
# When on, messages are logged instead of sent. Worth leaving on until the
# wording has been read on a real handset, since the recipients are real clients.
CLIENT_SMS_DRY_RUN = (os.getenv("CLIENT_SMS_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}

RINGCENTRAL_SMS_FROM = (os.getenv("RINGCENTRAL_SMS_FROM") or "").strip()

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
        "Not configured. Set RINGCENTRAL_SMS_FROM to an SMS-capable number on the "
        "RingCentral extension the app authenticates as, or set the TWILIO_* variables."
    )


def check_sending_setup() -> dict:
    """Ask RingCentral what this app can actually send from.

    Both of the things that stop a first send — the app having no SMS
    permission, and the configured number not belonging to the extension the
    app authenticates as — are invisible until a message is refused. This asks
    up front, so the answer arrives before a client is waiting on it.
    """
    setup: dict = {
        "provider": active_provider(),
        "dry_run": CLIENT_SMS_DRY_RUN,
        "configured_from": RINGCENTRAL_SMS_FROM or None,
        "extension": None,
        "numbers": [],
        "sms_scope": None,       # True, False, or None when it cannot be known
        "ok": False,
        "problem": None,
    }

    if setup["provider"] == "twilio":
        setup["ok"] = True
        setup["problem"] = None if TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER else \
            "Twilio is selected but neither TWILIO_FROM_NUMBER nor TWILIO_MESSAGING_SERVICE_SID is set."
        setup["ok"] = setup["problem"] is None
        return setup

    if not RINGCENTRAL_SMS_FROM:
        setup["problem"] = (
            "RINGCENTRAL_SMS_FROM is not set. The numbers listed below are the ones this "
            "app is allowed to send from — put one of them in that variable."
        )

    scopes = get_ringcentral_scopes()
    if scopes is not None:
        setup["sms_scope"] = any(scope.lower() == "sms" for scope in scopes)
        setup["scopes"] = sorted(scopes)

    try:
        response = ringcentral_api_get("account/~/extension/~")
        if response.status_code < 400:
            extension = response.json()
            setup["extension"] = {
                "id": extension.get("id"),
                "name": (extension.get("contact") or {}).get("firstName"),
                "extensionNumber": extension.get("extensionNumber"),
                "type": extension.get("type"),
            }
        else:
            setup["problem"] = f"RingCentral refused the extension lookup ({response.status_code}): {response.text.strip()[:300]}"
            return setup

        numbers = ringcentral_api_get("account/~/extension/~/phone-number", params={"perPage": 100})
        if numbers.status_code >= 400:
            setup["problem"] = f"RingCentral refused the phone-number lookup ({numbers.status_code}): {numbers.text.strip()[:300]}"
            return setup

        for record in numbers.json().get("records", []):
            features = [str(feature) for feature in (record.get("features") or [])]
            setup["numbers"].append({
                "phoneNumber": record.get("phoneNumber"),
                "display": format_phone_for_display(record.get("phoneNumber")),
                "usageType": record.get("usageType"),
                "label": record.get("label"),
                "sms": "SmsSender" in features,
                "mms": "MmsSender" in features,
            })
    except Exception as exc:
        setup["problem"] = f"Could not reach RingCentral: {type(exc).__name__}: {exc}"
        return setup

    sendable = [number for number in setup["numbers"] if number["sms"]]
    configured = normalize_phone(RINGCENTRAL_SMS_FROM)

    if setup["sms_scope"] is False:
        setup["problem"] = (
            "This RingCentral app does not have the SMS permission, so every send will be "
            "refused. Add SMS to the app in the RingCentral developer console and re-authorize it."
        )
    elif not RINGCENTRAL_SMS_FROM:
        pass  # the problem is already set above
    elif not configured:
        setup["problem"] = f"RINGCENTRAL_SMS_FROM is set to {RINGCENTRAL_SMS_FROM!r}, which is not a phone number."
    elif not sendable:
        setup["problem"] = (
            "This extension has no SMS-capable number. Assign one to the extension the app "
            "authenticates as, or point the app at an extension that has one."
        )
    elif not any(normalize_phone(number["phoneNumber"]) == configured for number in sendable):
        allowed = ", ".join(number["display"] for number in sendable)
        setup["problem"] = (
            f"{format_phone_for_display(configured)} is not one of the numbers this extension can "
            f"text from. RingCentral will refuse it. Numbers that will work: {allowed}."
        )
    else:
        setup["ok"] = True

    return setup


def _send_via_ringcentral(to_number: str, body: str) -> SendResult:
    access_token = get_ringcentral_access_token()
    if not access_token:
        return SendResult(ok=False, provider="ringcentral", error="No RingCentral access token available.")

    url = f"{RINGCENTRAL_PLATFORM_BASE_URL.rstrip('/')}/restapi/v1.0/account/~/extension/~/sms"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "from": {"phoneNumber": RINGCENTRAL_SMS_FROM},
            "to": [{"phoneNumber": to_number}],
            "text": body,
        },
        timeout=RINGCENTRAL_REQUEST_TIMEOUT,
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
        return SendResult(ok=True, provider="dry-run", message_id="dry-run", status="DryRun")

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

    if result.ok:
        logger.info("Texted %s via %s (message %s)", normalized_to, result.provider, result.message_id)
    else:
        logger.warning("Could not text %s via %s: %s", normalized_to, result.provider, result.error)
    return result
