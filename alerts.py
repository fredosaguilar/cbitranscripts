import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM") or SMTP_USER


def is_smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


# Kept for callers that only need to know whether mail can be sent at all
def is_email_configured() -> bool:
    return is_smtp_configured()


def send_email(subject: str, body_text: str, body_html: str | None = None,
               to_addresses: list[str] | None = None, from_address: str | None = None) -> bool:
    """Send to explicitly named recipients only.

    There is deliberately no fallback recipient: every message goes to the
    person it concerns, so a stray ALERT_EMAIL_TO cannot pull mail into a
    shared inbox.

    from_address exists for mail to clients, which should come from the address
    they would write back to rather than the mailbox the app authenticates as.
    The SMTP account still has to be allowed to send as it -- a provider that
    refuses an unauthorised From shows up here as a failed send, not a silent
    rewrite.
    """
    recipients = [address.strip() for address in (to_addresses or []) if address and address.strip()]
    if not is_smtp_configured():
        logger.info("SMTP not configured; skipping: %s", subject)
        return False
    if not recipients:
        logger.info("No recipient for message; skipping: %s", subject)
        return False

    sender = (from_address or "").strip() or ALERT_EMAIL_FROM
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        message.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(sender, recipients, message.as_string())
        logger.info("Sent email: %s", subject)
        return True
    except Exception:
        logger.exception("Failed to send email: %s", subject)
        return False


def send_email_result(subject: str, body_text: str, to_addresses: list[str],
                      from_address: str | None = None) -> tuple[bool, str]:
    """Send one email and say why if it did not go.

    The fire-and-forget version is right for notifying staff, where a failure
    belongs in the log. Mail to a client is not that: someone is standing at the
    screen having pressed Send, and needs to be told it did not go.
    """
    if not is_smtp_configured():
        return False, ("Email is not configured on the server. Set SMTP_HOST, SMTP_USER "
                       "and SMTP_PASSWORD in Railway.")
    recipients = [address.strip() for address in to_addresses if address and address.strip()]
    if not recipients:
        return False, "No email address to send to."

    sender = (from_address or "").strip() or ALERT_EMAIL_FROM
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(body_text, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(sender, recipients, message.as_string())
        logger.info("Sent email to %s: %s", ", ".join(recipients), subject)
        return True, f"Emailed {', '.join(recipients)}."
    except smtplib.SMTPSenderRefused as exc:
        logger.exception("Sender %s refused", sender)
        return False, (f"The mail server refused to send as {sender}. The SMTP account has to be "
                       f"allowed to send from that address. ({exc.smtp_code})")
    except smtplib.SMTPRecipientsRefused:
        logger.exception("Recipient refused: %s", recipients)
        return False, f"The mail server would not accept {', '.join(recipients)} as an address."
    except Exception as exc:
        logger.exception("Failed to send email: %s", subject)
        return False, f"The email could not be sent: {type(exc).__name__}: {exc}"


def send_email_async(subject: str, body_text: str, body_html: str | None = None,
                     to_addresses: list[str] | None = None):
    threading.Thread(
        target=send_email,
        args=(subject, body_text, body_html, to_addresses),
        name="email-sender",
        daemon=True,
    ).start()
