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
import threading
import time
import uuid
from email.utils import parseaddr

import broadcasts
import config
import renewal
import tariffs
import templates
from xui_client import XuiClient, XuiUnavailable, get_shared_client

# The transport, imported by name so that everything the panel and the tests
# already call on this module keeps working — including patching it, since the
# handlers below look these up as module globals at call time.
from mailer import SmtpSession  # noqa: F401
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
    scan_auth_servers,
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

def kept_device_limit(client_obj: dict, wanted: int, who: str) -> int:
    """
    The device limit a client actually has, with a warning when it is not the
    one that was asked for.

    A 3x-ui from before HWID limits takes the client and drops the field
    without a word. Nothing fails — the tariff simply goes on promising a limit
    nobody enforces, and the first sign of it would be a subscription shared
    round an office. So whoever creates a client reads it back through this,
    and tells the client what the panel kept rather than what the tariff says.
    """
    kept = XuiClient.device_limit(client_obj)
    wanted = int(wanted or 0)
    if wanted > 0 and kept is None:
        logger.warning(
            f"{who} was meant to be limited to {wanted} device(s), but this 3x-ui "
            f"does not know device limits at all — the client was created without "
            f"one. HWID limits need a recent 3x-ui; until the panel is updated the "
            f"tariff's device limit is not enforced.")
    elif wanted > 0 and kept != wanted:
        # A panel that knows the field and still did not keep it. 3x-ui applies
        # the limit only when the client went into every inbound asked for, so
        # this is what a half-finished creation looks like — not an old panel,
        # and blaming the version would send somebody to update for nothing.
        logger.warning(
            f"{who} was meant to be limited to {wanted} device(s), but 3x-ui "
            f"holds {kept}. The panel sets the limit only when the client was "
            f"added to every inbound, so the creation most likely failed on one "
            f"of them — check the client's inbounds, then set the limit on its "
            f"card.")
    return kept or 0


def kept_renewal(client_obj: dict, wanted: dict, who: str):
    """
    The auto-renewal a client actually has — what renewal.of_client() reads
    off it, None for a panel that does not say — with a warning when it is
    not the one that was asked for.

    The same trap as the device limit, with a costlier end. A panel that
    drops the fields creates the client all the same; the letter would promise
    a subscription that renews, and the first sign otherwise is somebody cut
    off on the day the term ends. So whoever creates a client reads it back
    through this, and the letter states what the panel holds.
    """
    kept = renewal.of_client(client_obj)
    if not (wanted or {}).get("mode"):
        return kept
    asked = renewal.panel_fields(wanted)
    if kept is None:
        logger.warning(
            f"{who} was meant to renew automatically ({asked}), but this 3x-ui "
            f"does not know auto-renewal at all — the client was created without "
            f"it, and the subscription will simply end with its term. Auto-renewal "
            f"needs a recent 3x-ui; until the panel is updated the tariff's renewal "
            f"does nothing.")
    elif not renewal.same(kept, wanted):
        # A panel that knows the fields and holds something else: what a
        # creation that failed part of the way looks like.
        logger.warning(
            f"{who} was meant to renew automatically ({asked}), but 3x-ui holds "
            f"{renewal.panel_fields(kept, always=True)}. The creation most likely "
            f"failed on one of the inbounds — check the client, then set the "
            f"renewal from its edit form.")
    elif not kept["active"]:
        logger.warning(
            f"{who} has auto-renewal set and no expiry for it to act on, so "
            f"3x-ui will never renew the subscription.")
    return kept


def send_welcome_email(email_addr: str, sub_url: str, expire_days: int = None,
                       limit_gb: int = None, renewed: bool = False, tariff: str = "",
                       limit_hwid: int = 0, renew: dict = None):
    """
    The welcome letter carrying the subscription link.

    One place serves both self-registration by code word and creating a client
    from the web panel, so that the letter is the same either way.

    `tariff` is the name shown beside the term and the limit. It is the name of
    the tariff the client is actually on — their group in 3x-ui — rather than
    the one the word they wrote would have given them: a letter that named a
    tariff somebody is not on would be worse than one naming none.

    `limit_hwid` is how many devices the client may use, and follows the same
    rule: what the client has in 3x-ui, not what a tariff says. 0 leaves the
    line out.

    `renew` is the auto-renewal, and the rule holds for it most of all: it is
    what renewal.of_client() read off the client, never the tariff's own
    setting, since a line saying the subscription renews is a promise.
    """
    if expire_days is None:
        expire_days = config.EXPIRE_DAYS
    if limit_gb is None:
        limit_gb = config.LIMIT_GB

    send_email_reply(email_addr, templates.welcome_subject(renewed),
                     templates.get_welcome_email(sub_url, expire_days, limit_gb,
                                                 renewed=renewed, tariff=tariff,
                                                 limit_hwid=limit_hwid, renew=renew))


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
    # Strictly, here and below: a panel that did not answer is not a panel
    # that knows no such client. Taking the one for the other meant trying to
    # create a client that might already exist, and then writing to say it
    # could not be done. The exception puts the letter off instead — see
    # _handle_letter.
    client_info = xui.find_client_by_email(email_addr, strict=True)

    if client_info:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_info.get('subId')}"
        # Their own tariff, not the one the word opens: an existing client keeps
        # what they have, and the letter has to say the same thing.
        send_welcome_email(email_addr, sub_url, renewed=True,
                           tariff=client_info.get("group") or "",
                           limit_hwid=XuiClient.device_limit(client_info) or 0,
                           renew=renewal.of_client(client_info))
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
        limit_hwid=tariff.get("limit_hwid", 0),
        renew=tariff.get("renew"),
    )
    if not success_uuid:
        logger.error(f"The panel refused to create a client for {email_addr}.")

    try:
        client_info = xui.find_client_by_email(email_addr, strict=True)
    except XuiUnavailable:
        # Created, and then the panel went quiet before it could be read back.
        # The letter is put off and next time takes the "already registered"
        # path above, which sends the link and spends nothing — so the code
        # has to be spent here, or an invitation for one lets in two.
        if success_uuid and code:
            tariffs.spend(code["word"], email_addr)
        raise

    if client_info:
        sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client_info.get('subId')}"
        send_welcome_email(email_addr, sub_url, tariff["expire_days"], tariff["limit_gb"],
                           tariff=tariff["name"],
                           limit_hwid=kept_device_limit(client_info, tariff.get("limit_hwid", 0),
                                                        f"Client {email_addr}"),
                           renew=kept_renewal(client_info, tariff.get("renew"),
                                              f"Client {email_addr}"))
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

    # Strictly: during an outage a registered client used to be told they had
    # never registered, which is a worse answer than a late one.
    client_info = xui.find_client_by_email(email_addr, strict=True)
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

        # The devices registered against the limit, when there is one. Asked
        # only then: without a limit 3x-ui registers nobody, and on a panel
        # from before such limits there is no such question to ask. A list the
        # panel would not give leaves None, and the letter then states the
        # limit alone rather than going unsent over it.
        device_limit = XuiClient.device_limit(client_info) or 0
        devices = None
        if device_limit:
            listed = xui.get_client_devices(client_info.get("email", email_addr))
            if listed is not None:
                devices = [XuiClient.device_label(d) for d in listed]

        send_email_reply(email_addr, templates.text("status.subject"),
                         templates.get_status_email(email_addr, is_active, up, down, total,
                                                    expiry_time,
                                                    tariff=(client_info.get("group") or "").strip(),
                                                    device_limit=device_limit,
                                                    devices=devices,
                                                    # Off the row already in
                                                    # hand: the list carries it.
                                                    renew=renewal.of_client(client_info)))
    else:
        send_email_reply(email_addr, templates.notice_subject("status_error"),
                         templates.get_notice("status_error"))
        logger.warning(f"Client {email_addr} was known, but 3x-ui did not find it when asked for status.")

def handle_help(email_addr: str):
    """Sending the help information."""
    xui = get_shared_client()
    # With the panel known to be away the letter goes without the link rather
    # than waiting ten seconds to learn the same thing: help is the one answer
    # that is worth having either way. `getattr`, since a stand-in for the
    # client need not know the question.
    away = getattr(xui, "recently_failed", lambda: False)()
    client_info = None if away else xui.find_client_by_email(email_addr)

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


def get_all_active_emails_from_xui(xui_client, strict: bool = False) -> list:
    """
    Asks 3x-ui for every client and keeps only the active ones.
    Returns a list of bare emails, extracted from the remark through parseaddr,
    fit for SMTP even when the remark is shaped as 'Name <email>'.

    :param strict: raise XuiUnavailable when the list could not be had, rather
                   than answering with nobody. A broadcast asks this way: with
                   the panel away it used to go out to no one and report
                   itself done.
    """
    clients = xui_client.get_all_clients()
    if clients is None and strict:
        # In the panel's own words where it has them: see XuiClient.unavailable.
        explain = getattr(xui_client, "unavailable", None)
        raise explain() if explain else XuiUnavailable("3x-ui does not answer")
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
                     kind="plain", job_id: str = None):
    """
    Broadcasts a message to the chosen clients, or to every active one.

    Every broadcast is written into `broadcasts` as it goes, here rather than
    by whoever asked for it: the panel's Broadcast page shows what is being
    sent from that record, and one started by mail has no other way of
    getting onto it.

    :param job_id: the record to write into, when the caller has opened one
                   already — the panel does, because it needs the id for the
                   page it sends the browser to before any letter has left.
                   Without it a record is opened here, as one started by mail.
    """
    if not subject:
        subject = templates.text("notice.broadcast_default_subject")

    if emails is None:
        emails = get_all_active_emails_from_xui(get_shared_client())

    if not emails:
        logger.info("No active clients to broadcast to.")
        if job_id:
            broadcasts.finish(job_id)
        return 0

    message = templates.get_broadcast_email(subject, broadcast_body, kind)
    job = job_id or broadcasts.start(subject, broadcast_body, len(emails), kind,
                                     source=broadcasts.MAIL)

    logger.info(f"Starting a broadcast to {len(emails)} clients...")
    success_count = 0
    failed_count = 0
    try:
        # One sign-in for the lot, not one a recipient: see SmtpSession. Each
        # letter is still a letter of its own, to one address — nobody's
        # address is shown to anybody else — and the pause between them stays.
        with SmtpSession() as session:
            for to_email in emails:
                failed_address = ""
                try:
                    time.sleep(0.5)
                    send_email_reply(to_email, subject, message, session=session)
                    success_count += 1
                except Exception as e:
                    failed_count += 1
                    failed_address = to_email
                    logger.error(f"Could not send the broadcast to {to_email}: {e}")
                broadcasts.progress(job, success_count, failed_count, failed_address)
            if session.connections > 1:
                logger.info(f"The broadcast took {session.connections} SMTP connections.")
    except BaseException as e:
        # Not a letter that failed — those are counted above — but the loop
        # itself. Said in the record, or the page would show a broadcast that
        # ended a third of the way through as one that simply finished.
        broadcasts.finish(job, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        broadcasts.finish(job)

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
    seen, found = set(), []
    for word in words or []:
        key = tariffs.normalise_word(word)
        if key in seen:
            continue
        spans = _word_spans(text, word)
        if spans:
            seen.add(key)
            found.append((word, spans))

    # A word that only ever appears as a piece of a longer word that is itself
    # a code was not written by anybody: AURORA inside AURORA-FAMILY. The
    # boundary contains_word draws lets punctuation stand inside a word, which
    # is what makes AURORA-2026 one word — and also what found the short word
    # whole inside the long one, since a hyphen is not a letter. The letter
    # then read as carrying two words, and whoever wrote the long one was asked
    # which subscription they meant. Standing alone somewhere as well, it still
    # counts, and the letter is as ambiguous as it looks.
    out = []
    for word, spans in found:
        longer = [other for candidate, others in found if candidate is not word
                  for other in others]
        if all(any(o[0] <= s[0] and s[1] <= o[1] and o != s for o in longer) for s in spans):
            continue
        out.append(word)
    return out


def _word_spans(text: str, word: str) -> list:
    """Where `word` stands in the text as a word of its own: (start, end) pairs."""
    word = str(word or "").strip()
    if not word:
        return []
    pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
    return [match.span() for match in re.finditer(pattern, text or "",
                                                  re.IGNORECASE | re.UNICODE)]


def process_message(msg_num, from_email: str, subject: str, body: str, mail_conn,
                    sender_name: str = "", authenticated: bool = False):
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

    `authenticated` is the mailbox server's word that the From address is real
    (inbox.sender_authenticated). Only /broadcast asks for it. Everything else
    a letter can do, it does to its own sender — a forged From gets a stranger
    a subscription link they did not ask for and nothing more — whereas a
    forged administrator used to get any text at all sent to every client,
    from the service's own address.
    """
    subject_clean = subject.strip()
    body_clean = body.strip()

    logger.info(f"Handling a letter from {from_email}. Subject: {subject_clean!r}")

    # An empty ADMIN_EMAIL means "no administrator set", not "matches an empty
    # sender": without an explicit check that is easy to miss.
    is_admin = bool(config.ADMIN_EMAIL) and from_email == config.ADMIN_EMAIL

    is_broadcast_cmd = subject_clean.lower().startswith("/broadcast") or body_clean.lower().startswith("/broadcast")

    if is_admin and is_broadcast_cmd:
        if not authenticated:
            _mark_seen(mail_conn, msg_num)
            logger.warning(f"A /broadcast came from {from_email}, but the mail server does not "
                           f"vouch for that address; nothing was sent. If the letter was yours, "
                           f"send the broadcast from the panel.")
            # To the administrator's real address, which is where a reply to
            # this From goes whoever wrote the letter: either their own
            # broadcast that did not go out, or news that somebody tried.
            send_email_reply(from_email, templates.notice_subject("broadcast_unverified"),
                             templates.get_notice("broadcast_unverified"))
            return

        broadcast_content = ""
        if subject_clean.lower().startswith("/broadcast"):
            broadcast_content = subject_clean[10:].strip()

        if not broadcast_content:
            if body_clean.lower().startswith("/broadcast"):
                broadcast_content = body_clean[10:].strip()
            else:
                broadcast_content = body_clean

        if not broadcast_content:
            _mark_seen(mail_conn, msg_num)
            send_email_reply(from_email, templates.notice_subject("broadcast_empty"),
                             templates.get_notice("broadcast_empty"))
            return

        # Who it goes to is settled now, while the letter is still unread: with
        # 3x-ui away this raises, the letter is put off like any other, and the
        # broadcast goes out when the panel is back. It used to be asked after
        # the letter was marked read — and a panel that did not answer meant a
        # broadcast to nobody, reported to the administrator as done.
        recipients = get_all_active_emails_from_xui(get_shared_client(), strict=True)

        _mark_seen(mail_conn, msg_num)
        # And the sending itself is not done here. This runs in the poll loop,
        # and a broadcast takes minutes: for all of them the bot read no
        # mailbox, and whoever wrote a code word meanwhile waited for the last
        # of somebody else's letters to leave.
        _in_background(_broadcast_by_mail, broadcast_content, recipients, from_email)
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


def _in_background(work, *args):
    """
    Runs `work(*args)` on a thread of its own and comes straight back.

    A daemon thread: a broadcast in flight when the process is stopped stops
    with it, as one started from the panel does. A name of its own so that the
    tests can run the work in place and wait for nothing.
    """
    threading.Thread(target=work, args=args, name="broadcast-by-mail", daemon=True).start()


def _broadcast_by_mail(content: str, recipients: list, report_to: str):
    """
    A broadcast asked for by letter: the sending, then the report to whoever
    asked. Runs off the poll loop — see process_message.

    Nothing may escape from here: there is no caller left to hear it, only a
    thread that would die with a traceback and an administrator who was never
    told how their broadcast went.
    """
    sent, broke_off, job_id = 0, False, None
    try:
        if recipients:
            # Opened here rather than left to the sending, so that what is
            # reported afterwards is read back by its id. Looked for by its
            # text it could be another broadcast altogether — the same words
            # sent again after a failure, which is exactly when one is.
            subject = templates.text("notice.broadcast_default_subject")
            job_id = broadcasts.start(subject, content, len(recipients), source=broadcasts.MAIL)
            sent = handle_broadcast(content, subject=subject, emails=recipients, job_id=job_id)
    except Exception as e:
        logger.error(f"The broadcast asked for by {report_to} broke off: {e}", exc_info=True)
        broke_off = True
        if job_id:
            broadcasts.finish(job_id, error=f"{type(e).__name__}: {e}")
            sent = (broadcasts.get(job_id) or {}).get("sent", 0)
    try:
        if broke_off:
            # Not "finished": said as what it was, with how far it got.
            send_email_reply(report_to, templates.notice_subject("broadcast_cut"),
                             templates.get_notice("broadcast_cut", count=sent,
                                                  total=len(recipients), code_text=content))
        else:
            send_email_reply(report_to, templates.notice_subject("broadcast_done"),
                             templates.get_notice("broadcast_done", count=sent, code_text=content))
    except Exception as e:
        logger.error(f"Could not report the broadcast back to {report_to}: {e}")


def _handle_letter(*args, **kwargs):
    """
    process_message, as the poll loop is given it.

    The one thing added is the translation of an outage. The loop knows
    nothing of 3x-ui and should not; what it does know is TryLater — leave the
    letter unread, do not count the attempt, come back next cycle. Without
    this a panel away for a minute cost every letter that arrived in it: three
    failed attempts and the letter was marked read and given up on.
    """
    try:
        return process_message(*args, **kwargs)
    except XuiUnavailable as e:
        # In the panel's own words, and saying which kind of trouble it is: a
        # panel that is away comes back, one that refuses the bot's token
        # waits for somebody to change a setting.
        raise _inbox.TryLater(str(e) or "3x-ui does not answer",
                              needs_somebody=getattr(e, "denied", False)) from e


def describe_tariffs(known: list) -> list:
    """
    The tariffs as the start-up line names them: how many words each has, and
    not the words themselves. A code word is what lets somebody in, and the log
    is a file on disk and a page in the panel — both are read, copied into bug
    reports and screenshotted by people who were never meant to hold one.
    """
    described = []
    for tariff in known:
        words = sum(1 for c in tariffs.codes_for(tariff["id"]) if c["enabled"])
        described.append(f"{tariff['name']} (code words: {words})" if words
                         else f"{tariff['name']} (no code word)")
    return described


def check_mail():
    """One pass over the mailbox, with this module's handler."""
    return _inbox.check_mail(_handle_letter)


def main():
    import os
    import settings
    import applog
    import email_texts
    # As in run.py, and for the same reason: the log names everybody who wrote.
    os.umask(0o077)
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
        logger.info(f"Tariffs: {len(known)} — " + "; ".join(describe_tariffs(known)))
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
