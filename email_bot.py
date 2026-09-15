"""
What the bot does about a letter: the commands, the registrations, the letters
it sends back.

The transport lives elsewhere — mailer.py gets a letter out over SMTP and
inbox.py reads the mailbox and runs the poll loop. This module is the middle:
it is handed a letter that somebody wrote and decides what it means.

The names the rest of the project has always imported from here still resolve:
the panel, run.py and the tests reach for email_bot.send_email_reply and
email_bot.check_mail, and both are re-exported below rather than moved out of
reach.
"""
import logging
import re
import time
import uuid
from email.utils import parseaddr

import config
import tariffs
import templates
from xui_client import XuiClient, get_shared_client

# The transport, imported by name so that everything the panel and the tests
# already call on this module keeps working — including patching it, since the
# handlers below look these up as module globals at call time.
from mailer import (  # noqa: F401
    build_message,
    open_smtp,
    probe_smtp,
    render_template,
    send_email_via,
    send_email_reply,
    send_gotify_notification,
)
from inbox import (  # noqa: F401
    FAILURES_BEFORE_ALARM,
    SCAN_LIMIT,
    decode_mime_header,
    get_email_body,
    mail_health,
    probe_imap,
    scan_sender_names,
    sender_name_from,
    _mark_seen,
)
import inbox as _inbox

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
                       limit_gb: int = None, renewed: bool = False, tariff: str = ""):
    """
    The welcome letter carrying the subscription link.

    One place serves both self-registration by code word and creating a client
    from the web panel, so that the letter is the same either way.

    `tariff` is the name shown beside the term and the limit. It is the name of
    the tariff the client is actually on — their group in 3x-ui — rather than
    the one the word they wrote would have given them: a letter that named a
    tariff somebody is not on would be worse than one naming none.
    """
    if expire_days is None:
        expire_days = config.EXPIRE_DAYS
    if limit_gb is None:
        limit_gb = config.LIMIT_GB

    send_email_reply(email_addr, templates.welcome_subject(renewed),
                     templates.get_welcome_email(sub_url, expire_days, limit_gb,
                                                 renewed=renewed, tariff=tariff))


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
        # Their own tariff, not the one the word opens: an existing client keeps
        # what they have, and the letter has to say the same thing.
        send_welcome_email(email_addr, sub_url, renewed=True,
                           tariff=client_info.get("group") or "")
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
        send_welcome_email(email_addr, sub_url, tariff["expire_days"], tariff["limit_gb"],
                           tariff=tariff["name"])
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


def check_mail():
    """One pass over the mailbox, with this module's handler."""
    return _inbox.check_mail(process_message)


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
