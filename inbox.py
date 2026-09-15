"""
Reading: the IMAP end of the bot — the poll loop and the mailbox it polls.

Named inbox rather than mailbox because the standard library owns that name,
and a module of ours beside it would shadow it for the whole process.

The other half of what email_bot used to be. This module fetches letters,
decides which are worth handing on, and hands each to a callback; what that
callback does with a letter is not its business. That is also why check_mail
takes the handler as an argument rather than importing it: the loop would
otherwise depend on the commands, and the commands already depend on this.

The letter is marked read only after the handler has dealt with it. It used to
be the other way round, and the gap between the two cost whole registrations.
"""
import email
import imaplib
import logging
import re
import socket
import time
from email.header import decode_header
from email.utils import parseaddr

import config

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


# --- Clearing out the mailbox ---
# Read letters go to the Trash rather than to nowhere: what a cleanup took can
# still be looked at, and a mistake in a schedule is then a mistake and not a
# loss. The folder is asked of the server rather than guessed, because its name
# depends on the provider and on the language the mailbox was created in.

# RFC 6154 marks the Trash with an attribute; these are the names to fall back
# on when the server does not use it.
TRASH_NAMES = ("Trash", "INBOX.Trash", "Deleted Items", "Deleted Messages",
               "[Gmail]/Trash", "Корзина", "INBOX.Корзина", "Удалённые")

# Letters are moved in batches: a mailbox left alone for a year holds thousands,
# and one command carrying every uid of them is a line no server enjoys.
CLEANUP_BATCH = 200


def _folder_name(line) -> str:
    """The mailbox name out of one LIST line, quotes and all removed."""
    text = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
    # ( attributes ) "delimiter" "name" — the name is the tail, and it is the
    # only part that may hold spaces, so splitting from the right is enough.
    parts = text.rsplit('"', 2)
    if len(parts) == 3 and parts[1]:
        return parts[1]
    return text.split()[-1].strip('"')


def trash_folder(mail):
    """
    The mailbox's Trash folder, or an empty string when it has none.

    Asked for by its special-use attribute first: the folder is called Корзина on
    one provider and [Gmail]/Trash on another, and a list of names would be a
    list of the providers somebody happened to try.
    """
    try:
        status, lines = mail.list()
    except Exception as e:
        logger.warning(f"Could not list the mailbox folders: {e}")
        return ""
    if status != "OK" or not lines:
        return ""

    names = []
    for line in lines or []:
        if line is None:
            continue
        text = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
        name = _folder_name(line)
        if "\\Trash" in text or "\\trash" in text.lower():
            return name
        names.append(name)

    lowered = {name.lower(): name for name in names}
    for candidate in TRASH_NAMES:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return ""


def _move_to_trash(mail, uids, folder) -> int:
    """Moves the letters to the folder, by MOVE where the server has it."""
    quoted = '"%s"' % folder.replace('"', '\\"')
    moved = 0
    has_move = "MOVE" in (getattr(mail, "capabilities", ()) or ())
    for start in range(0, len(uids), CLEANUP_BATCH):
        batch = b",".join(uids[start:start + CLEANUP_BATCH]).decode("ascii")
        try:
            if has_move:
                status, _ = mail.uid("MOVE", batch, quoted)
            else:
                # The old way, and still the only way on a server without
                # RFC 6851: copy, flag, expunge. The copy has to succeed before
                # anything is flagged, or the letters would be deleted from the
                # Inbox without arriving anywhere.
                status, _ = mail.uid("COPY", batch, quoted)
                if status == "OK":
                    mail.uid("STORE", batch, "+FLAGS", "(\\Deleted)")
                    mail.expunge()
        except Exception as e:
            logger.error(f"Could not move letters to {folder}: {e}")
            break
        if status != "OK":
            logger.error(f"The server refused to move letters to {folder}: {status}")
            break
        moved += len(uids[start:start + CLEANUP_BATCH])
    return moved


def cleanup_due(now=None) -> bool:
    """Whether the mailbox is due for a clear-out."""
    if not getattr(config, "MAIL_CLEANUP_ENABLED", False):
        return False
    last = float(getattr(config, "MAIL_CLEANUP_LAST_AT", 0) or 0)
    if not last:
        # Never run, which is also the first poll after somebody switched it on:
        # the mailbox is cleared now rather than in a month's time.
        return True
    days = max(1, int(getattr(config, "MAIL_CLEANUP_DAYS", 30) or 1))
    return (now or time.time()) - last >= days * 86400


def cleanup(mail) -> int:
    """
    Moves the read letters to the Trash. Returns how many were moved.

    Read means the bot has dealt with it: the flag is set after the handler has
    finished, not before, so an unread letter is one still owed an answer — and
    after an IMAP outage that may be a registration that has not happened yet.
    Those are left where they are whatever the schedule says.
    """
    folder = trash_folder(mail)
    if not folder:
        logger.error("The mailbox has no Trash folder that can be found, so the "
                     "cleanup did nothing. Nothing was deleted.")
        return 0
    try:
        status, response = mail.uid("SEARCH", None, "SEEN")
    except Exception as e:
        logger.error(f"Could not search the mailbox for read letters: {e}")
        return 0
    if status != "OK":
        logger.error(f"Could not search the mailbox for read letters: {status}")
        return 0

    uids = response[0].split() if response and response[0] else []
    if not uids:
        logger.info("Mailbox cleanup: nothing read to clear out.")
        return 0

    moved = _move_to_trash(mail, uids, folder)
    logger.info(f"Mailbox cleanup: {moved} read letter(s) moved to {folder}.")
    return moved


def _remember_cleanup(when: float):
    """Writes down when the cleanup ran, so a restart keeps the schedule."""
    try:
        import settings
        settings.save({"MAIL_CLEANUP_LAST_AT": when})
    except Exception as e:
        # The letters are already moved; losing the timestamp costs one extra
        # cleanup, which has nothing left to move anyway.
        logger.warning(f"Could not write down when the mailbox was cleaned: {e}")


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

def check_mail(handle):
    """
    Connects over IMAP, looks for unread letters and hands each on.

    `handle` is called for every letter worth acting on, with the same
    arguments email_bot.process_message takes. Passed in rather than imported:
    the loop would otherwise depend on the commands, and the commands already
    depend on the loop's own helpers.
    """
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

                handle(num, from_email, subject, body, mail, sender_name)
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

        if cleanup_due():
            # On the connection that is already open, and only once the letters
            # of this cycle have been handled: a letter is moved after it has
            # been answered, never instead.
            phase = "clearing out the mailbox"
            cleanup(mail)
            _remember_cleanup(time.time())

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
