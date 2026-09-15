import re
import time
import logging
import imaplib
import smtplib
import email
import uuid
import requests
from email.charset import QP, Charset
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from email.header import decode_header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
import tariffs
import templates
from xui_client import XuiClient, get_shared_client


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_email_body(msg):
    """Pulls the text content out of an email message object."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body += part.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception as e:
                    logger.debug(f"Error decoding the plain text part: {e}")
            elif content_type == "text/html" and "attachment" not in content_disposition and not body:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body += part.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception as e:
                    logger.debug(f"Error decoding the html part: {e}")
    else:
        content_type = msg.get_content_type()
        if content_type in ("text/plain", "text/html"):
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = msg.get_payload(decode=True).decode(charset, errors="ignore")
            except Exception as e:
                logger.debug(f"Error decoding a simple message: {e}")
    return body

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


def probe_imap(server_host: str, port: int, user: str, password: str) -> int:
    """
    Checks the mailbox login and returns how many letters are in the inbox.

    It connects exactly the way check_mail does, over SSL. A check that reached
    the server by another route would be lying: it could pass where the bot's
    own polling fails.
    """
    mail = imaplib.IMAP4_SSL(server_host, int(port), timeout=10)
    try:
        mail.login(user, password)
        status, data = mail.select("inbox")
        if status != "OK":
            raise RuntimeError("signed in, but the Inbox does not open")
        count = int(data[0]) if data and data[0] else 0
    except Exception:
        # A stalled server would keep the check waiting through a second
        # timeout for a goodbye it is not going to answer.
        _disconnect(mail, graceful=False)
        raise
    _disconnect(mail, graceful=True)
    return count


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

def build_comment(sender_name: str = "") -> str:
    """
    Prepares the sender's name for the client's separate `comment` field.

    The name used to be glued to the address as 'Name <email>' and written into
    the email field, but the panel accepts only a limited character set there
    and answered "client email contains an invalid character: >". Registration
    therefore failed for every sender whose From header carried a name — which
    is nearly all of them. Address and name now live in separate fields.
    """
    if not config.REMARK_INCLUDE_NAME:
        return ""
    clean = (sender_name or "").replace("\r", " ").replace("\n", " ")
    # Angle brackets come out of the name: the panel already refused them in the
    # email field, and trusting that comment allows them would be optimistic.
    return clean.replace("<", "").replace(">", "").strip()


# A 3x-ui client's `email` field is an identifier with a pared-down set of
# allowed characters, not a free-form string.
_EMAIL_FIELD_ALLOWED = re.compile(r"[^A-Za-z0-9@._+-]")


def build_client_email(email_addr: str) -> str:
    """
    Prepares the address for a 3x-ui client's `email` field: the bare address
    alone, without a name and without characters the panel refuses.
    """
    raw = (email_addr or "").strip()
    # When "Name <email>" arrives, take the address itself rather than gluing
    # everything together.
    _, parsed = parseaddr(raw)
    candidate = (parsed or raw).strip()
    cleaned = _EMAIL_FIELD_ALLOWED.sub("", candidate)
    if cleaned != raw:
        logger.warning(f"Address {raw!r} reduced to {cleaned!r} for the email field in 3x-ui.")
    return cleaned

def send_welcome_email(email_addr: str, sub_url: str, expire_days: int = None,
                       limit_gb: int = None, renewed: bool = False):
    """
    The welcome letter carrying the subscription link.

    One place serves both self-registration by code word and creating a client
    from the web panel, so that the letter is the same either way.
    """
    if expire_days is None:
        expire_days = config.EXPIRE_DAYS
    if limit_gb is None:
        limit_gb = config.LIMIT_GB

    send_email_reply(email_addr, templates.welcome_subject(renewed),
                     templates.get_welcome_email(sub_url, expire_days, limit_gb, renewed=renewed))


def send_personal_email(email_addr: str, subject: str, message_body: str, kind="plain"):
    """
    A personal letter to a single client, sent from the web panel.
    The styling matches a broadcast, and the text is escaped in the template.
    """
    send_email_reply(email_addr, subject,
                     templates.get_broadcast_email(subject, message_body, kind))


def handle_registration(email_addr: str, sender_name: str = "", tariff: dict = None,
                        code: dict = None):
    """
    Handles registering a client in the panel.

    The tariff says what the client gets — traffic, term, inbounds — and is read
    once, here. From this moment the values live on the client in 3x-ui, so a
    tariff edited tomorrow leaves this client exactly as it is.

    Somebody already registered who writes another tariff's word gets their link
    again and nothing else: moving a client between tariffs is the
    administrator's decision, not something anybody who learned a more generous
    word can do for themselves.
    """
    if not tariff:
        logger.error(f"Registration for {email_addr} without a tariff; refusing. "
                     f"This is a bug — the caller must resolve the code word first.")
        return

    xui = get_shared_client()
    client_info = xui.find_client_by_email(email_addr)

    if client_info:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_info.get('subId')}"
        send_welcome_email(email_addr, sub_url, renewed=True)
        logger.info(f"Client {email_addr} is already registered; the link was sent again. "
                    f"The {tariff['name']!r} tariff was not applied — an existing client keeps what it has.")
        return

    client_email = build_client_email(email_addr)
    comment = build_comment(sender_name)
    client_uuid = str(uuid.uuid4())

    logger.info(f"Registering a new client: {client_email} (name={comment!r}) "
                f"on the {tariff['name']!r} tariff ...")
    success_uuid, success_inbounds = xui.add_client(
        email=client_email,
        client_uuid=client_uuid,
        limit_gb=tariff["limit_gb"],
        expire_days=tariff["expire_days"],
        inbound_ids=tariff["inbound_ids"],
        comment=comment,
        # The tariff's name is the client's group in 3x-ui. That is where the
        # answer to "who is on what" lives — in the panel that owns the client,
        # not in a file of ours that could disagree with it.
        group=tariff["name"],
    )
    if not success_uuid:
        logger.error(f"The panel refused to create a client for {email_addr}.")

    client_info = xui.find_client_by_email(email_addr)

    if client_info:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_info.get('subId')}"
        send_welcome_email(email_addr, sub_url, tariff["expire_days"], tariff["limit_gb"])
        logger.info(f"Client {email_addr} registered successfully on {tariff['name']!r}.")
        # The code is spent only now. Burning it before the client exists would
        # lose an invitation to a 3x-ui that happened to be unreachable.
        if code:
            tariffs.spend(code["word"], email_addr)
        fields = {
            "email": email_addr,
            "name": comment or "",
            "service": config.SERVICE_NAME,
            "tariff": tariff["name"],
        }
        send_gotify_notification(
            title=render_template(config.GOTIFY_TITLE, **fields),
            message=render_template(config.GOTIFY_MESSAGE, **fields),
        )
    else:
        send_email_reply(email_addr, templates.notice_subject("create_error"),
                         templates.get_notice("create_error"))
        logger.error(f"Could not register client {email_addr} in 3x-ui.")

def handle_status(email_addr: str):
    """A request for traffic figures and subscription status."""
    xui = get_shared_client()

    client_info = xui.find_client_by_email(email_addr)
    if not client_info:
        send_email_reply(email_addr, templates.notice_subject("not_registered"),
                         templates.get_notice("not_registered"))
        return

    traffic = xui.get_client_traffic(client_info.get("email", email_addr))
    if traffic:
        up = traffic.get("up", 0)
        down = traffic.get("down", 0)
        total = traffic.get("total", 0)
        expiry_time = traffic.get("expiryTime", 0)
        enable = traffic.get("enable", True)
        is_user_enable = client_info.get("enable", True)

        is_active = enable and is_user_enable

        send_email_reply(email_addr, templates.text("status.subject"),
                         templates.get_status_email(email_addr, is_active, up, down, total,
                                                    expiry_time,
                                                    tariff=(client_info.get("group") or "").strip()))
    else:
        send_email_reply(email_addr, templates.notice_subject("status_error"),
                         templates.get_notice("status_error"))
        logger.warning(f"Client {email_addr} was known, but 3x-ui did not find it when asked for status.")

def handle_help(email_addr: str):
    """Sending the help information."""
    xui = get_shared_client()
    client_info = xui.find_client_by_email(email_addr)

    sub_url = None
    if client_info:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_info.get('subId')}"

    send_email_reply(email_addr, templates.text("help.subject"),
                     templates.get_help_email(email_addr, sub_url))

def handle_unknown(email_addr: str, subject_received: str):
    """The reply to an unknown command, or a letter without the code word."""
    # The subject is somebody else's text, but there is no need to escape it by
    # hand: it goes into the template as a value, and Jinja escapes it itself.
    send_email_reply(email_addr, templates.notice_subject("unknown"),
                     templates.get_notice("unknown", subject=(subject_received or "").strip()))


def get_all_active_emails_from_xui(xui_client) -> list:
    """
    Asks 3x-ui for every client and keeps only the active ones.
    Returns a list of bare emails, extracted from the remark through parseaddr,
    fit for SMTP even when the remark is shaped as 'Name <email>'.
    """
    clients = xui_client.get_all_clients()
    if not clients:
        logger.warning("Could not fetch the client list from 3x-ui, or it is empty.")
        return []

    active_emails = []
    skipped = 0

    for client_obj in clients:
        if client_obj.get("enable") is not True:
            continue
        bare = xui_client.extract_bare_email(client_obj.get("email"))
        if bare:
            active_emails.append(bare)
        else:
            # A client added to the panel by hand: the remark field holds an
            # arbitrary identifier rather than an address, so there is nowhere
            # to send to.
            skipped += 1

    if skipped:
        logger.info(f"Active clients skipped for having no email in the remark: {skipped}.")

    return active_emails

def handle_broadcast(broadcast_body: str, subject: str = None, emails: list = None,
                     on_progress=None, kind="plain"):
    """
    Broadcasts a message to the chosen clients, or to every active one.

    :param on_progress: an optional callback (sent, failed) invoked after each
                        recipient — the web panel draws its progress from it.
    """
    if not subject:
        subject = templates.text("notice.broadcast_default_subject")

    if emails is None:
        emails = get_all_active_emails_from_xui(get_shared_client())

    if not emails:
        logger.info("No active clients to broadcast to.")
        return 0

    message = templates.get_broadcast_email(subject, broadcast_body, kind)

    logger.info(f"Starting a broadcast to {len(emails)} clients...")
    success_count = 0
    failed_count = 0
    for to_email in emails:
        try:
            time.sleep(0.5)
            send_email_reply(to_email, subject, message)
            success_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Could not send the broadcast to {to_email}: {e}")

        if on_progress is not None:
            try:
                on_progress(success_count, failed_count)
            except Exception as e:
                logger.debug(f"Error in the broadcast progress callback: {e}")

    logger.info(f"Broadcast finished. Sent successfully: {success_count}/{len(emails)}.")
    return success_count

def decode_mime_header(value: str) -> str:
    """A header as a person would read it, whatever it was encoded with."""
    parts = []
    for chunk, charset in decode_header(value or ""):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(str(chunk))
    return " ".join(parts).strip()


def sender_name_from(from_header: str) -> tuple:
    """
    (address, name) out of a From header, or (address, "") when it carries none.

    A display name that is only the address again — which some clients send —
    counts as no name: writing it into the comment would say nothing the email
    field does not already say.
    """
    decoded = decode_mime_header(from_header)
    name, address = parseaddr(decoded)
    address = (address or "").strip().lower()
    name = (name or "").replace("\r", " ").replace("\n", " ").strip()
    if name.lower() == address:
        name = ""
    return address, name


# Headers alone, and PEEK so the scan does not mark anything read: an unhandled
# registration must not be swallowed by somebody pressing a button in settings.
_HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM)])"
# A ceiling, so a mailbox with years of unrelated mail in it cannot hold the
# panel open indefinitely. The newest letters are the ones that matter.
SCAN_LIMIT = 5000
SCAN_BATCH = 200


def scan_sender_names(limit: int = SCAN_LIMIT) -> dict:
    """
    Every address that has written, with the name it last signed itself as.

    Reads the whole inbox, newest last, so a later letter overwrites an earlier
    one and the most recent spelling of a name wins. Raises on a mailbox that
    cannot be opened; an empty mailbox is not an error and comes back as {}.
    """
    if not config.IMAP_USER or not config.IMAP_PASSWORD:
        raise RuntimeError("the mailbox is not set up")

    found = {}
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_SERVER, config.IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.login(config.IMAP_USER, config.IMAP_PASSWORD)
        mail.select("inbox", readonly=True)

        status, response = mail.search(None, "ALL")
        if status != "OK":
            raise RuntimeError(f"the mailbox could not be searched: {status}")
        numbers = response[0].split() if response and response[0] else []
        if not numbers:
            return {}
        numbers = numbers[-limit:]

        for start in range(0, len(numbers), SCAN_BATCH):
            batch = numbers[start:start + SCAN_BATCH]
            status, data = mail.fetch(b",".join(batch).decode(), _HEADERS)
            if status != "OK" or not data:
                continue
            for item in data:
                # Every other element is the literal; the rest are the ")" bits.
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                try:
                    raw = item[1].decode("utf-8", errors="ignore")
                    header = email.message_from_string(raw).get("From", "")
                    address, name = sender_name_from(header)
                except Exception:
                    # One unreadable letter is not a reason to abandon the rest.
                    continue
                if address and name:
                    found[address] = name
        return found
    finally:
        _disconnect(mail, graceful=True)


def name_mismatches(clients: list, found: dict) -> list:
    """
    The clients whose name in 3x-ui does not match the one their letters carry.

    Pure on purpose: the reading of the mailbox is awkward to test and this is
    the part with the judgement in it. A client with no name at all counts as a
    mismatch — filling one in is the usual reason for doing this — while a client
    nobody has written to, or one whose name already matches, does not appear.
    """
    rows = []
    for client_obj in clients or []:
        remark = client_obj.get("email", "") or ""
        address = XuiClient.extract_bare_email(remark)
        if not address:
            continue
        name = found.get(address)
        if not name:
            continue
        current = (client_obj.get("comment") or "").strip()
        if current == name:
            continue
        rows.append({
            "uuid": XuiClient.client_key(client_obj),
            "email": address,
            "current": current,
            "name": name,
        })
    rows.sort(key=lambda r: r["email"])
    return rows


def _mark_seen(mail_conn, msg_num):
    """Marks the letter read, and does not let that failure lose the letter."""
    try:
        mail_conn.store(msg_num, '+FLAGS', '\\Seen')
    except Exception as e:
        logger.warning(f"Could not mark letter #{msg_num} as read: {e}")


# Where a reply stops being what the sender wrote and starts being what they
# are replying to. Everything from the first of these lines onward is quoted
# text, and a code word found in it was written by us, not by them — the
# welcome letter carries the word that registered them, so a plain "thanks"
# in reply used to read as a fresh registration once several words existed.
QUOTE_LINE = re.compile(
    r"^\s*(?:>"
    r"|-{2,}\s*(?:original message|forwarded message|пересылаемое сообщение)"
    r"|(?:on|в|вт|ср|чт|пт|сб|вс|пн)\b.{0,120}?(?:wrote|writes|пишет|написал\w*):\s*$"
    r"|(?:from|от|sent|отправлено|to|кому|subject|тема)\s*:\s"
    r")",
    re.IGNORECASE | re.MULTILINE)


def readable_text(subject: str, body: str) -> str:
    """
    The part of a letter its sender actually typed: the subject, plus the body
    down to the first sign of quoted text.

    Matching against the whole body is what the single code word could afford —
    there was one word, and finding it anywhere meant the same thing. With a
    word per tariff a quotation is a real hazard: a reply to the welcome letter
    carries the word that opened it, and would otherwise register the sender
    again, or against the wrong tariff.
    """
    body = str(body or "")
    cut = QUOTE_LINE.search(body)
    if cut:
        body = body[:cut.start()]
    return f"{str(subject or '')}\n{body}"


def contains_word(text: str, word: str) -> bool:
    """
    Whether the text carries `word` as a word of its own.

    Substring matching was fine for one code word chosen by the administrator;
    with several it is a trap. START sits inside RESTART, a short word sits
    inside a long one, and neither was written by anybody meaning to register.
    The boundary is "no letter, digit or underscore either side" rather than
    the usual word boundary, so that a word containing punctuation —
    AURORA-2026 — still matches as one piece instead of falling apart.
    """
    word = str(word or "").strip()
    if not word:
        return False
    pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
    return re.search(pattern, text or "", re.IGNORECASE | re.UNICODE) is not None


def find_words(text: str, words) -> list:
    """
    Every distinct code word the text carries, in the order the words are given.

    More than one means the letter is ambiguous and nothing should be guessed:
    a registration against the wrong tariff hands out the wrong limits, and the
    sender is right there to be asked.
    """
    seen, out = set(), []
    for word in words or []:
        key = tariffs.normalise_word(word)
        if key in seen or not contains_word(text, word):
            continue
        seen.add(key)
        out.append(word)
    return out


def process_message(msg_num, from_email: str, subject: str, body: str, mail_conn, sender_name: str = ""):
    """
    Reads a letter and carries out whatever it asks for.

    The letter is marked read *after* it has been dealt with, not before. It used
    to be the other way round, and the gap between the two cost whole
    registrations: 3x-ui unreachable, SMTP refusing, the process restarted — the
    letter was already `\\Seen` by then, the next cycle never saw it again, and
    somebody who wrote the code word simply got nothing back. Left unread it is
    picked up on the next pass, and the commands are safe to repeat: registering
    an address that already exists sends the link again, and status and help only
    ever send a letter.

    /broadcast is the exception and is marked before it runs. It is the one
    command that is not safe to repeat — a mass letter going out twice is worse
    than one that did not go out at all and can simply be sent again by hand.
    """
    subject_clean = subject.strip()
    body_clean = body.strip()

    logger.info(f"Handling a letter from {from_email}. Subject: {subject_clean!r}")

    # An empty ADMIN_EMAIL means "no administrator set", not "matches an empty
    # sender": without an explicit check that is easy to miss.
    is_admin = bool(config.ADMIN_EMAIL) and from_email == config.ADMIN_EMAIL

    is_broadcast_cmd = subject_clean.lower().startswith("/broadcast") or body_clean.lower().startswith("/broadcast")

    if is_admin and is_broadcast_cmd:
        broadcast_content = ""
        if subject_clean.lower().startswith("/broadcast"):
            broadcast_content = subject_clean[10:].strip()

        if not broadcast_content:
            if body_clean.lower().startswith("/broadcast"):
                broadcast_content = body_clean[10:].strip()
            else:
                broadcast_content = body_clean

        _mark_seen(mail_conn, msg_num)
        if broadcast_content:
            sent_count = handle_broadcast(broadcast_content)
            send_email_reply(from_email, templates.notice_subject("broadcast_done"),
                             templates.get_notice("broadcast_done", count=sent_count,
                                                  code_text=broadcast_content))
        else:
            send_email_reply(from_email, templates.notice_subject("broadcast_empty"),
                             templates.get_notice("broadcast_empty"))
        return

    # Only what the sender typed, and only whole words: see readable_text and
    # contains_word above for why both matter once every tariff has a word.
    text = readable_text(subject_clean, body_clean)
    hits = find_words(text, tariffs.live_words())

    if len(hits) > 1:
        # Two tariffs in one letter. Guessing would hand out the wrong limits,
        # and the person who wrote it is right there to be asked.
        logger.info(f"The letter from {from_email} carries several code words "
                    f"({', '.join(hits)}); asking which one is meant.")
        send_email_reply(from_email, templates.notice_subject("ambiguous"),
                         templates.get_notice("ambiguous", words=", ".join(hits)))
    elif hits:
        matched = tariffs.match(hits[0])
        if matched:
            handle_registration(from_email, sender_name, matched[0], matched[1])
        else:
            # Between the scan and the lookup the code was revoked or spent.
            handle_unknown(from_email, subject_clean)
    elif contains_word(text, "/start"):
        # /start carries no tariff of its own. With a single tariff there is
        # nothing to choose and it still means "let me in", which is how it
        # behaved before tariffs existed; with several, only a word can say
        # which one is meant.
        only = tariffs.all_tariffs()
        if len(only) == 1:
            code = tariffs.match(next(iter(tariffs.live_words()), "")) if tariffs.live_words() else None
            handle_registration(from_email, sender_name, only[0],
                                code[1] if code and code[0]["id"] == only[0]["id"] else None)
        else:
            handle_unknown(from_email, subject_clean)
    elif contains_word(text, "/status"):
        handle_status(from_email)
    elif contains_word(text, "/help"):
        handle_help(from_email)
    else:
        handle_unknown(from_email, subject_clean)

    # Only now: anything that raised above leaves the letter unread for the
    # next cycle, which is the whole point.
    _mark_seen(mail_conn, msg_num)

# How long to wait for the mail server on any single operation. Mail services
# stall now and then, and a check that gives up is cheap: the letters stay
# unread and the next cycle collects them.
IMAP_TIMEOUT = 15

# A stall is ordinary; a run of them means the mailbox is out of reach. Until
# this many checks have failed in a row it is not worth waking anybody.
FAILURES_BEFORE_ALARM = 3

# Failed checks since the last one that worked.
_consecutive_failures = 0
# When the mailbox was last read through without trouble, and what went wrong
# the last time it did not. The panel shows both: a mail loop that has quietly
# stopped working looks exactly like a mailbox nobody is writing to.
_last_ok_at = None
_last_error = ""

# A letter that throws on every attempt would otherwise be retried for ever, now
# that the read flag waits for success. Counted by Message-ID and kept in
# memory: a restart gives it a fresh chance, which is what one wants after
# fixing whatever it tripped over.
MAX_ATTEMPTS = 3
_attempts = {}


def mail_health() -> dict:
    """How the mail loop is doing, for the dashboard."""
    return {
        "configured": bool(config.IMAP_USER and config.IMAP_PASSWORD),
        "last_ok_at": _last_ok_at,
        "failures": _consecutive_failures,
        "error": _last_error,
    }


def _disconnect(mail, graceful: bool):
    """
    Hangs up, politely when there is somebody still listening.

    After a timeout there is not: logout() sends BYE and then waits for the
    answer, so a server that has just failed to reply costs a second timeout —
    and a close() before it a third, with the poll loop standing still for all
    of them. When the exchange has already broken, drop the socket instead.
    """
    if mail is None:
        return
    if not graceful:
        try:
            mail.shutdown()
        except Exception:
            pass
        return
    try:
        mail.close()
    except Exception:
        pass
    try:
        mail.logout()
    except Exception:
        pass


def check_mail():
    """Connects over IMAP, looks for unread letters and handles them."""
    global _consecutive_failures, _last_ok_at, _last_error

    if not config.IMAP_USER or not config.IMAP_PASSWORD:
        logger.error("IMAP credentials are not configured; skipping the mail check.")
        return

    mail = None
    # Named so a failure says which call hung, rather than leaving the phrase
    # "the read operation timed out" to stand on its own.
    phase = "connecting"
    reached_the_end = False
    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_SERVER, config.IMAP_PORT, timeout=IMAP_TIMEOUT)
        phase = "signing in"
        mail.login(config.IMAP_USER, config.IMAP_PASSWORD)
        phase = "opening the Inbox"
        mail.select("inbox")

        phase = "searching for unread letters"
        status, response = mail.search(None, "UNSEEN")
        messages = response[0].split() if status == "OK" else []
        if status != "OK":
            logger.error(f"Could not search the mailbox: {status}")
        elif not messages:
            # Once per POLL_INTERVAL_SECONDS and almost always true — in the log
            # buffer this is just noise.
            logger.debug("No new unread letters.")

        phase = "handling the letters"
        if messages:
            logger.info(f"New letters found to handle: {len(messages)}")
        for num in messages:
            msg_id = None
            try:
                status, data = mail.fetch(num, "(RFC822)")
                if status != "OK" or not data:
                    logger.error(f"Could not fetch letter #{num}")
                    continue
                
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                msg_id = msg.get("Message-ID") or None

                from_header = msg.get("From", "")
                # Decode the From header so the sender's name comes out right.
                from_parts = []
                for decoded_str, charset in decode_header(from_header):
                    if isinstance(decoded_str, bytes):
                        from_parts.append(decoded_str.decode(charset or "utf-8", errors="ignore"))
                    else:
                        from_parts.append(str(decoded_str))
                from_decoded = " ".join(from_parts).strip()

                sender_name, from_email = parseaddr(from_decoded)
                from_email = from_email.strip().lower()
                if not from_email:
                    logger.warning(f"Could not extract the sender address for letter #{num}")
                    continue

                subject_parts = []
                subject_header = msg.get("Subject", "")
                for decoded_str, charset in decode_header(subject_header):
                    if isinstance(decoded_str, bytes):
                        subject_parts.append(decoded_str.decode(charset or "utf-8", errors="ignore"))
                    else:
                        subject_parts.append(str(decoded_str))
                subject = " ".join(subject_parts).strip()

                body = get_email_body(msg)

                process_message(num, from_email, subject, body, mail, sender_name)
                if msg_id:
                    _attempts.pop(msg_id, None)

            except Exception as e:
                if msg_id:
                    tries = _attempts.get(msg_id, 0) + 1
                    _attempts[msg_id] = tries
                else:
                    # Nothing to count by. Rather than risk retrying it for ever,
                    # this one attempt is treated as the last.
                    tries = MAX_ATTEMPTS
                logger.error(f"Error handling letter #{num} (attempt {tries}): {e}", exc_info=True)
                if tries >= MAX_ATTEMPTS:
                    logger.error(f"Letter #{num} failed {tries} times and is being marked read "
                                 f"so the loop can move on. Nothing was answered to it.")
                    _mark_seen(mail, num)
                    _attempts.pop(msg_id, None)

        reached_the_end = True
        _last_ok_at = time.time()
        _last_error = ""
        if _consecutive_failures:
            logger.info(f"The mailbox answers again, after {_consecutive_failures} "
                        f"failed check(s) in a row.")
            _consecutive_failures = 0

    except Exception as e:
        _consecutive_failures += 1
        detail = f"{type(e).__name__}: {e}".strip(": ")
        _last_error = f"{phase}: {detail}"
        message = (f"The mail check failed while {phase}: {detail}. "
                   f"Failed checks in a row: {_consecutive_failures}.")
        # One stall costs a single cycle and nothing else: unread letters stay
        # unread and the next check takes them. Only a run of them is a fault
        # worth raising to the dashboard.
        if _consecutive_failures < FAILURES_BEFORE_ALARM:
            logger.warning(message)
        else:
            logger.error(message, exc_info=True)
    finally:
        _disconnect(mail, graceful=reached_the_end)

def main():
    import settings
    import applog
    import email_texts
    settings.load()
    email_texts.load()
    applog.install()

    logger.info("Checking the connection to 3x-ui...")
    xui = get_shared_client()
    if xui.login():
        logger.info("Connection to 3x-ui: OK.")
    else:
        logger.error("Connection to 3x-ui: FAILED. The bot is running, but requests to the panel may fail.")

    logger.info(f"Bot started. Mail poll interval: {config.POLL_INTERVAL_SECONDS} seconds.")
    known = tariffs.all_tariffs()
    if known:
        described = []
        for tariff in known:
            words = [c["word"] for c in tariffs.codes_for(tariff["id"]) if c["enabled"]]
            described.append(f"{tariff['name']} ({', '.join(words) if words else 'no word'})")
        logger.info(f"Tariffs: {len(known)} — " + "; ".join(described))
    else:
        logger.error("No tariffs are set up; registration will be refused until one exists.")

    while True:
        try:
            check_mail()
        except Exception as e:
            logger.error(f"Error in the bot loop: {e}", exc_info=True)
        time.sleep(config.POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
