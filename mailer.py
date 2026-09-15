"""
Sending: the SMTP end of the bot, and the push that goes with a registration.

Split out of email_bot, which had grown to hold three unrelated jobs — how a
letter is sent, how the mailbox is read, and what the bot does about what it
finds. This half knows nothing about tariffs, commands or clients: it is handed
a subject and a rendered letter and gets them out of the building.

The letters themselves are assembled in templates.py from the editable texts;
what arrives here is already an Email(html, text).
"""
import logging
import smtplib
from email.charset import QP, Charset
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

import config

logger = logging.getLogger(__name__)

# utf-8 with quoted-printable for the text part
_QP_UTF8 = Charset("utf-8")
_QP_UTF8.body_encoding = QP


def _sender_from(smtp_user: str = None, service_name: str = None) -> str:
    """
    The From header with a display name: the recipient sees the service name
    rather than a bare address. Cyrillic is encoded per RFC 2047 by formataddr
    itself.

    None of this reaches the envelope from, which still carries the plain
    address — an SMTP server would refuse anything else.

    The parameters are there for the panel's connection check: it sends with
    whatever is in the fields right now, not yet saved into config.
    """
    user = config.SMTP_USER if smtp_user is None else smtp_user
    _, address = parseaddr(user or "")
    name = (config.SERVICE_NAME if service_name is None else service_name or "").strip()
    if not name or not address:
        return user
    return formataddr((name, address), charset="utf-8")


def _sender_domain(smtp_user: str = None) -> str:
    """The sender's domain for the Message-ID; otherwise the hostname lands there."""
    user = config.SMTP_USER if smtp_user is None else smtp_user
    _, address = parseaddr(user or "")
    domain = address.rsplit("@", 1)[-1].strip()
    return domain or "localhost"

def build_message(to_email: str, subject: str, message,
                  smtp_user: str = None, service_name: str = None):
    """
    Assembles the finished MIME letter.

    :param message: a templates.Email (text plus HTML), or a string of HTML.

    Inside multipart/alternative the parts run from plain to rich: the client
    takes the last one it can display. The text part is not only for such
    clients — a letter without one fares worse with spam filters.
    """
    if isinstance(message, str):
        html_content, text_content, images = message, "", None
    else:
        html_content, text_content = message.html, message.text
        images = getattr(message, "images", None)

    # A letter with pictures needs one more layer: multipart/related holds the
    # HTML together with what it refers to by Content-ID, and multipart/-
    # alternative goes inside it. The headers belong on whichever part is
    # outermost, so the two are built first and addressed afterwards.
    body = MIMEMultipart('alternative')
    msg = MIMEMultipart('related') if images else body
    if images:
        msg.attach(body)
    msg['Subject'] = subject
    msg['From'] = _sender_from(smtp_user, service_name)
    msg['To'] = to_email
    # Without Date and Message-ID the letter looks suspect to spam filters:
    # rspamd charges 3.5 points for it (MISSING_DATE + MISSING_MID).
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=_sender_domain(smtp_user))
    # What sent it is ordinary courtesy, and a clue when a complaint is looked
    # into. The version is not decoration: without it XM_UA_NO_VERSION fires.
    msg['X-Mailer'] = f"{config.APP_NAME} {config.APP_VERSION}"

    # quoted-printable instead of base64 for both parts: MIME_BASE64_TEXT fires
    # on HTML too, and the letter stays readable in its source.
    if text_content:
        body.attach(MIMEText(text_content, 'plain', _QP_UTF8))
    body.attach(MIMEText(html_content, 'html', _QP_UTF8))

    for cid, data in (images or {}).items():
        if not data:
            continue
        part = MIMEImage(data)
        # The angle brackets are what the standard asks for in the header; the
        # src in the HTML is written without them.
        part.add_header('Content-ID', f'<{cid}>')
        # "inline" rather than "attachment", so the client draws it in the
        # letter instead of hanging a paperclip on it.
        part.add_header('Content-Disposition', 'inline', filename=f'{cid}.png')
        msg.attach(part)
    return msg

def open_smtp(server_host: str, port: int, timeout: int = 10):
    """
    The SMTP connection. 465 goes straight to SSL, any other port is raised
    through STARTTLS: in neither case does the password travel in the clear.
    """
    if int(port) == 465:
        return smtplib.SMTP_SSL(server_host, int(port), timeout=timeout)
    server = smtplib.SMTP(server_host, int(port), timeout=timeout)
    server.starttls()
    return server

def probe_smtp(server_host: str, port: int, user: str, password: str):
    """Checks that SMTP accepts the connection and the login. Sends nothing."""
    server = open_smtp(server_host, port)
    try:
        server.login(user, password)
    finally:
        try:
            server.quit()
        except Exception:
            pass

def send_email_via(server_host: str, port: int, user: str, password: str,
                   to_email: str, subject: str, message, service_name: str = None):
    """
    Sends a letter with the parameters given, bypassing config.

    The panel's connection check needs this: it works with whatever is typed in
    the fields, and must neither save those values nor swap config out for the
    duration — the bot is alive in its own thread alongside.
    """
    msg = build_message(to_email, subject, message,
                        smtp_user=user, service_name=service_name)
    # The envelope carries the plain address: with a display name the server
    # would refuse it.
    _, envelope = parseaddr(user or "")
    server = open_smtp(server_host, port)
    try:
        server.login(user, password)
        server.sendmail(envelope or user, [to_email], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass

def send_email_reply(to_email: str, subject: str, message):
    """Sends a letter over SMTP using the settings from config."""
    try:
        send_email_via(config.SMTP_SERVER, config.SMTP_PORT,
                       config.SMTP_USER, config.SMTP_PASSWORD,
                       to_email, subject, message)
        logger.info(f"Letter sent to {to_email}. Subject: {subject}")
    except Exception as e:
        logger.error(f"Could not send the letter to {to_email}. Error: {e}")
        raise

class _SafeFormat(dict):
    """An unknown substitution stays in the text rather than breaking the send."""

    def __missing__(self, key):
        return "{" + key + "}"

def render_template(text: str, **values) -> str:
    """
    Substitutes values into the notification template.
    A malformed template — an unclosed brace and the like — has no business
    breaking a registration, so on error the text comes back unsubstituted.
    """
    try:
        return (text or "").format_map(_SafeFormat(values))
    except (ValueError, IndexError, KeyError) as e:
        logger.warning(f"Could not parse the notification template {text!r}: {e}")
        return text or ""

def send_gotify_notification(title: str, message: str):
    """Sends a notification to Gotify, when the URL and token are configured."""
    if not config.GOTIFY_URL or not config.GOTIFY_TOKEN:
        logger.debug("Gotify is not configured; skipping the notification.")
        return

    url = f"{config.GOTIFY_URL.rstrip('/')}/message"
    headers = {
        "X-Gotify-Key": config.GOTIFY_TOKEN
    }
    payload = {
        "title": title,
        "message": message,
        "priority": config.GOTIFY_PRIORITY
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Notification sent to Gotify.")
        else:
            logger.error(f"Could not send the Gotify notification. Status: {response.status_code}, body: {response.text}")
    except Exception as e:
        logger.error(f"Error while sending the Gotify notification: {e}")
