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
import time
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

class PlainText(str):
    """
    A letter that is text and nothing else — what a person typed into the
    Support reply box, with the letter it answers quoted underneath.

    A type of its own because build_message takes a bare string to be HTML,
    which is what every string it had ever been handed was. The reply was a
    bare string too, and so went out as text/html with no text part: its line
    breaks collapsed into one paragraph in the recipient's mail client, and
    whatever markup stood in the quoted original — a stranger's text — was
    markup again in a letter from Support's address.
    """


def build_message(to_email: str, subject: str, message,
                  smtp_user: str = None, service_name: str = None, extra_headers: dict = None,
                  automatic: bool = True):
    """
    Assembles the finished MIME letter.

    :param message: a templates.Email (text plus HTML), a string of HTML, or
        a PlainText — which is sent as text alone.
    :param extra_headers: set after everything else, so a caller building a
        reply can add In-Reply-To/References — the two headers that thread it
        under the letter it answers in the recipient's own mail client.
    :param automatic: False for a letter a person typed — a reply from the
        Support mailbox. Everything else here is written by the program and
        says so; a person's answer marked that way is what the other side's
        autoresponder, ticket system or filter is being told to disregard.

    Inside multipart/alternative the parts run from plain to rich: the client
    takes the last one it can display. The text part is not only for such
    clients — a letter without one fares worse with spam filters.
    """
    if isinstance(message, PlainText):
        html_content, text_content, images = "", str(message), None
    elif isinstance(message, str):
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
    # Every letter here is written by a program, and saying so is what keeps
    # two programs from answering each other for ever (RFC 3834): a vacation
    # autoresponder that honours the header stays quiet, and inbox.py refuses
    # to answer anything carrying it — which includes a letter of our own that
    # found its way back. The second header is the same request in the only
    # dialect Exchange listens to.
    if automatic:
        msg['Auto-Submitted'] = "auto-generated"
        msg['X-Auto-Response-Suppress'] = "All"
    for name, value in (extra_headers or {}).items():
        if value:
            msg[name] = value

    # quoted-printable instead of base64 for both parts: MIME_BASE64_TEXT fires
    # on HTML too, and the letter stays readable in its source.
    if text_content:
        body.attach(MIMEText(text_content, 'plain', _QP_UTF8))
    if html_content:
        body.attach(MIMEText(html_content, 'html', _QP_UTF8))
    if not text_content and not html_content:
        # Nothing to say is still a letter with a body: a multipart with no
        # parts at all is something mail servers are entitled to refuse.
        body.attach(MIMEText("", 'plain', _QP_UTF8))

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

def _connection_lost(error: Exception) -> bool:
    """
    Whether a failure to send means the connection is gone, as against the
    server having said no to this one letter.

    The distinction is the whole of SmtpSession's judgement: a lost connection
    is worth one fresh attempt at the same letter, and a refused recipient is
    not — the answer would be the same and the session is still good. Every
    smtplib exception is an OSError, so the order of the questions matters.
    421 is the server saying it is closing the channel, whichever command it
    was answering at the time.
    """
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return any(code == 421 for code, _ in (error.recipients or {}).values())
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code == 421
    if isinstance(error, smtplib.SMTPException):
        return False
    # What is left of OSError: a reset, a timeout, a TLS failure.
    return isinstance(error, OSError)


class SmtpSession:
    """
    One sign-in to the SMTP server for a run of letters — a broadcast.

    Each letter used to open a connection, sign in, send and hang up. For a
    registration that is one letter and costs nothing. For a broadcast it was
    a sign-in for every recipient, which is slow, and which a mail server may
    count against the account: several limit how often one may sign in, and a
    broadcast to a hundred clients is a hundred sign-ins in a minute.

    It is handed to send_email_reply, which hands it to send_email_via; the
    connection is opened there, on the first letter, with whatever credentials
    that call was given. So nothing here knows about config, and a stand-in
    for send_email_via — the stand keeps letters in a folder — never causes a
    connection at all.

    Used as a context manager, so the connection is closed however the run
    ends. Not safe to share between threads: one broadcast, one session.
    """

    # After this many letters the connection is replaced before the server
    # does it for us. Servers cap how much one connection may send, each at a
    # figure of its own; this is under the ones that are published.
    LETTERS_PER_CONNECTION = 50
    # After a connection could not be opened, how long before another is
    # tried. Until then every letter fails at once with the same error. A
    # server that is down costs ten seconds a try, and a broadcast to two
    # hundred used to mean two hundred tries — over half an hour of a thread
    # doing nothing, to learn what the first try said.
    RECONNECT_PAUSE = 30

    def __init__(self):
        self._server = None
        self._sent = 0
        # How many connections the run has taken; one, unless something broke
        # or the run was a long one. For the log and the tests.
        self.connections = 0
        # The server would not take the login. Not asked again for the rest of
        # the run: the point of one sign-in a broadcast is not to hammer the
        # server with them, and a wrong password hammered is how an account
        # gets locked.
        self._refused = None
        # The last failure to connect, and when another try is allowed.
        self._unreachable = None
        self._retry_at = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        """Says goodbye, if there is anybody to say it to."""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.quit()
        except Exception:
            pass

    def _abandon(self):
        """Drops a connection that is no longer there, without the goodbye it cannot hear."""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.close()
        except Exception:
            pass

    def deliver(self, connect, envelope: str, to_email: str, text: str):
        """
        Sends one letter over the connection this session holds, opening it
        with `connect()` when there is none.

        A connection that turns out to be gone is replaced once and the letter
        sent again: a broadcast takes minutes, and a server that hangs up half
        way through is ordinary. Once, because a second failure is not the
        connection's fault any more. The cost is that a letter the server had
        taken before the line dropped may arrive twice — one letter, against
        the rest of the broadcast not arriving at all.
        """
        if self._server is not None and self._sent >= self.LETTERS_PER_CONNECTION:
            self.close()
        for attempt in (1, 2):
            if self._server is None:
                # A connection that cannot be opened is not tried a second
                # time for the same letter: the second try at sending exists
                # for a line that dropped, and this one never held.
                self._open(connect)
            try:
                self._server.sendmail(envelope, [to_email], text)
                self._sent += 1
                return
            except Exception as e:
                lost = _connection_lost(e)
                if lost:
                    self._abandon()
                if not lost or attempt == 2:
                    raise
                logger.warning(f"The SMTP connection was lost in the middle of a run of letters "
                               f"({type(e).__name__}: {e}); connecting again and sending the "
                               f"letter for {to_email} once more.")

    def _open(self, connect):
        """Opens the connection, or raises what the last attempt to did, without trying."""
        if self._refused is not None:
            raise self._refused
        if self._unreachable is not None and time.monotonic() < self._retry_at:
            raise self._unreachable
        try:
            self._server = connect()
        except smtplib.SMTPAuthenticationError as e:
            self._refused = e
            raise
        except Exception as e:
            self._unreachable = e
            self._retry_at = time.monotonic() + self.RECONNECT_PAUSE
            raise
        self._unreachable = None
        self._sent = 0
        self.connections += 1


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
                   to_email: str, subject: str, message, service_name: str = None,
                   extra_headers: dict = None, automatic: bool = True,
                   session: SmtpSession = None):
    """
    Sends a letter with the parameters given, bypassing config.

    The panel's connection check needs this: it works with whatever is typed in
    the fields, and must neither save those values nor swap config out for the
    duration — the bot is alive in its own thread alongside. The Support
    mailbox's replies use it for the same reason: a second identity, sent
    without ever touching config.SMTP_*, which stays the bot's alone.

    :param session: an SmtpSession to send over, for a run of letters. Without
        one the letter gets a connection of its own, opened and closed here.
    """
    msg = build_message(to_email, subject, message,
                        smtp_user=user, service_name=service_name, extra_headers=extra_headers,
                        automatic=automatic)
    # The envelope carries the plain address: with a display name the server
    # would refuse it.
    _, envelope = parseaddr(user or "")

    def signed_in():
        server = open_smtp(server_host, port)
        try:
            server.login(user, password)
        except Exception:
            # Not left half open for the server to time out on.
            try:
                server.close()
            except Exception:
                pass
            raise
        return server

    if session is not None:
        session.deliver(signed_in, envelope or user, to_email, msg.as_string())
        return msg

    server = signed_in()
    try:
        server.sendmail(envelope or user, [to_email], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return msg

def send_email_reply(to_email: str, subject: str, message, keep_copy: bool = True,
                     session: SmtpSession = None):
    """
    Sends a letter over SMTP using the settings from config.

    :param keep_copy: False for a letter that only exists to be looked at, not
        answered — the sample sends on the Settings > Letters tab, say. Those
        share this function because they want the same logging and error
        handling as a real letter, but they are not correspondence, and the
        Sent folder in the Mail panel should not fill up with them.
    :param session: an SmtpSession, when this letter is one of a run: see
        SmtpSession. Passed on only when there is one, so that whatever stands
        in for send_email_via need not know the word.
    """
    try:
        msg = send_email_via(config.SMTP_SERVER, config.SMTP_PORT,
                             config.SMTP_USER, config.SMTP_PASSWORD,
                             to_email, subject, message,
                             **({"session": session} if session is not None else {}))
        logger.info(f"Letter sent to {to_email}. Subject: {subject}")
    except Exception as e:
        logger.error(f"Could not send the letter to {to_email}. Error: {e}")
        raise
    if keep_copy:
        _keep_copy(msg)


def _keep_copy(msg):
    """
    Hands the letter just sent to the Sent folder, without waiting on it.

    SMTP keeps nothing, so without this the panel's Sent folder would show only
    what the provider happened to file by itself. The copy is queued, never
    awaited: the letter has already gone, and neither a slow mailbox nor a
    failing one has any business with whether a registration succeeded.
    Imported here rather than at the top, because this module is otherwise
    only about sending and the folders are a reading concern.
    """
    if msg is None:
        return
    try:
        import mailfolders
        mailfolders.save_sent_copy("bot", msg.get("Message-ID", ""), msg.as_string().encode("utf-8"))
    except Exception as e:
        logger.warning(f"Could not queue a copy of the letter for the Sent folder: {e}")

class _SafeFormat(dict):
    """An unknown substitution stays in the text rather than breaking the send."""

    def __missing__(self, key):
        return "{" + key + "}"

def render_template(text: str, **values) -> str:
    """
    Substitutes values into the notification template.
    A malformed template — an unclosed brace and the like — has no business
    breaking a registration, so on error the text comes back unsubstituted.

    On any error. The three str.format raises for a template that will not
    parse were caught at first, and `{status.x}` is none of them: it parses,
    and then asks a string for an attribute it has not got. The text is typed
    into a settings field by a person, and what it can be made to raise is
    whatever str.format can.
    """
    try:
        return (text or "").format_map(_SafeFormat(values))
    except Exception as e:
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
