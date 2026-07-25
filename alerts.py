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
ALERT_EMAIL_TO = [
    address.strip()
    for address in (os.getenv("ALERT_EMAIL_TO") or "").split(",")
    if address.strip()
]


def is_email_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and ALERT_EMAIL_TO)


def send_email(subject: str, body_text: str, body_html: str | None = None) -> bool:
    if not is_email_configured():
        logger.info("Email not configured (set SMTP_HOST/SMTP_USER/SMTP_PASSWORD/ALERT_EMAIL_TO); skipping: %s", subject)
        return False

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ", ".join(ALERT_EMAIL_TO)
    message.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        message.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(ALERT_EMAIL_FROM, ALERT_EMAIL_TO, message.as_string())
        logger.info("Sent email: %s", subject)
        return True
    except Exception:
        logger.exception("Failed to send email: %s", subject)
        return False


def send_email_async(subject: str, body_text: str, body_html: str | None = None):
    threading.Thread(
        target=send_email,
        args=(subject, body_text, body_html),
        name="email-sender",
        daemon=True,
    ).start()
