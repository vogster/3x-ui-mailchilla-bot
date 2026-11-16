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
import base64
import email
import imaplib
import logging
import re
import socket
import time
from email.header import decode_header
from email.utils import parseaddr

import config
import periodic

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


class TryLater(Exception):
    """
    A handler's way of saying "not now, and it is not this letter's fault".

    3x-ui being away is the case it exists for. Any other exception counts
    against the letter and after MAX_ATTEMPTS it is marked read and given up on
    — right for a letter that trips the code, and exactly wrong for an outage,
    which would otherwise eat every registration that arrived during it. A
    letter put off this way stays unread and uncounted, and the rest of the
    cycle is skipped: whatever stopped this one will stop the next.
    """


# --- Letters that must not be answered ---
# The bot replies to everything, and so do other machines. A vacation
# autoresponder, a bounce, a newsletter: each gets "the command was not
# understood", and the ones that answer back get it again, for as long as both
# sides are plugged in. RFC 3834 is the agreement that stops this — a machine
# marks what it sends and does not reply to what another machine marked.

# Sender local parts no person writes from.
_ROBOT_SENDERS = frozenset({
    "mailer-daemon", "postmaster", "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply", "bounce", "bounces",
})
# Headers whose mere presence says a machine sent this.
_ROBOT_HEADERS = ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond",
                  "X-Autoresponse")


def automatic_reason(msg, from_email: str) -> str:
    """
    Why a letter looks machine-sent, or "" for one a person wrote.

    The reason is for the log: "ignored" with nothing beside it is no help to
    an administrator wondering where somebody's letter went.
    """
    auto = str(msg.get("Auto-Submitted") or "").strip().lower()
    # "no" is the one value that means a person — RFC 3834 defines it so.
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    precedence = str(msg.get("Precedence") or "").strip().lower()
    if precedence in ("bulk", "junk", "list", "auto_reply"):
        return f"Precedence: {precedence}"
    for header in _ROBOT_HEADERS:
        if msg.get(header):
            return header
    # X-Auto-Response-Suppress is deliberately not on the list, though we send
    # it ourselves. It asks for no out-of-office reply, which is not what a
    # subscription link is, and it is set by software on the sender's side that
    # nobody here can vouch for: a person whose mail carried it would write the
    # code word and hear nothing, with no trace but a line in the log.
    # A bounce travels with an empty envelope sender, precisely so that nothing
    # can be sent back to it.
    if str(msg.get("Return-Path") or "").strip() == "<>":
        return "an empty Return-Path"
    local = (from_email or "").split("@", 1)[0]
    if local in _ROBOT_SENDERS:
        return f"the sender is {local}"
    return ""


# The marks above are a courtesy the other side may not extend, and a person
# can flood a mailbox as well as a machine can. So there is a ceiling too:
# this many answered letters from one address within the window, and the rest
# are marked read in silence until it has passed. Generous for a person —
# nobody asks for their status ten times an hour — and the end of any loop.
REPLY_LIMIT = 10
REPLY_WINDOW = 60 * 60
# (address, proven) -> the times its letters were answered, oldest first. In
# memory: a restart forgives everybody, which costs one more round at worst.
#
# Proven and unproven letters are counted apart (see sender_authenticated).
# The address is only what the From header says, and counted together ten
# forged letters would have silenced the real owner of it for an hour — their
# registration marked read and never answered, or the administrator's own
# broadcast dropped. A forger cannot send a proven letter, so cannot touch the
# count the real sender is held to.
_answered = {}


def _recent(key, now: float) -> list:
    return [t for t in _answered.get(key, ()) if now - t < REPLY_WINDOW]


def over_limit(address: str, proven: bool = False, now: float = None) -> bool:
    """Whether this sender has already had all the answers one window allows."""
    now = time.time() if now is None else now
    return len(_recent((address, bool(proven)), now)) >= REPLY_LIMIT


def note_answered(address: str, proven: bool = False, now: float = None):
    """
    Counts one handled letter against its sender.

    Called after the handler has finished, never before: a letter put off or
    failed is seen again on the next cycle, and counting each sight of it would
    throttle somebody for the bot's own trouble.
    """
    now = time.time() if now is None else now
    key = (address, bool(proven))
    _answered[key] = _recent(key, now) + [now]
    # Senders are strangers and there is no end of them; drop whoever has
    # nothing left inside the window so the map cannot grow for ever.
    if len(_answered) > 1000:
        for stale in [k for k in _answered if not _recent(k, now)]:
            del _answered[stale]


# --- Whether the sender is who the From header says ---
# Anybody can type any address into From. What cannot be typed is the verdict
# the receiving server adds after checking SPF, DKIM and DMARC for itself.

# The verdict is read by its grammar and not searched for by pattern, because
# half of what is in it was written by the sender. The server copies the
# envelope address into `smtp.mailfrom=` as it stands, and an address may
# legally be dmarc=pass@evil.test — or, quoted, hold spaces and semicolons. A
# search for "dmarc=pass" anywhere in the header found it there and believed
# it, beside the server's own dmarc=fail.

_RESULT = re.compile(r"\s*(dmarc|dkim|spf)\s*=\s*([a-z]+)(?![\w.=-])")
# name=value, where a value is one run of quoted strings and unspaced text: a
# quoted address is taken whole, with whatever it has been made to spell.
_PROPERTY = re.compile(r'([a-z0-9._-]+)\s*=\s*((?:"(?:[^"\\]|\\.)*"|[^\s"])+)')
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def _split_results(header: str):
    """
    The header cut at its top-level semicolons, comments removed — or None when
    a quotation or a comment never closes.

    A semicolon inside a quoted string or a comment does not separate anything,
    which is the whole reason this is not `header.split(";")`. Quotes are
    honoured inside comments too, which the grammar does not ask for: servers
    put the sender's address into a comment unescaped, and a quoted address
    with a ")" in it would otherwise end the comment where its author chose.
    An unbalanced header is one somebody has been at, and is not read at all.
    """
    parts, current, depth, quoted = [], [], 0, False
    i = 0
    while i < len(header):
        ch = header[i]
        if ch == "\\" and i + 1 < len(header):
            if not depth:
                current.append(header[i:i + 2])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
            if not depth:
                current.append(ch)
        elif quoted:
            if not depth:
                current.append(ch)
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            current.append(" ")
        elif depth:
            pass
        elif ch == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if quoted or depth:
        return None
    parts.append("".join(current))
    return parts


def _domain(value) -> str:
    """The domain out of `example.com`, `@example.com` or `user@example.com`; "" for anything else."""
    domain = str(value or "").rsplit("@", 1)[-1].strip(".")
    return domain if _DOMAIN.match(domain) else ""


def _aligned(domain: str, sender_domain: str) -> bool:
    """The same domain, or one under the other — DMARC's relaxed alignment."""
    if not domain or not sender_domain:
        return False
    return (domain == sender_domain
            or domain.endswith("." + sender_domain)
            or sender_domain.endswith("." + domain))


# What the name of a server looks like in the header: a host name, as a rule.
# Held to that, so the setting cannot be made to match something a header
# merely spells along the way.
_AUTHSERV = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z")

# When a second header under the trusted name was last reported, so that a
# mailbox being sent such letters by the hundred says so once an hour.
_repeat_warned_at = 0.0
REPEAT_WARNING_EVERY = 60 * 60


_ASCII_SPACE = re.compile(r"[ \t\r\n]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _flat(header) -> str:
    """
    A header as it is read here: unfolded, single-spaced, lower case — and all
    three in ASCII alone.

    str.split() and str.lower() know Unicode, which is one kindness too many:
    a no-break space after a name would count as a space and the Kelvin sign
    as a k, so a header no mail server would take for its own — and so none
    would remove — came out of here spelling the trusted name exactly. What is
    not ASCII is left as it stands, and matches nothing.
    """
    return _ASCII_SPACE.sub(" ", str(header)).strip(" ").translate(_ASCII_LOWER)


# A header begins with the server's name, then at most a version number, then
# the semicolon. In that shape and no other: quoted, or with a comment on
# either side, the grammar allows the name too — but the server's own header
# has one shape, a filter that removes impostors compares against that shape,
# and every other spelling is one the filter may have let through.
_NAMED = re.compile(r"([a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)(?: [0-9]+)? ?;")


def _authserv_of(header: str) -> str:
    """
    The name of the server a flattened header was written under — the
    authserv-id, the first thing in it — or "" when it does not begin with one
    in the shape a server writes its own.
    """
    found = _NAMED.match(header)
    return found.group(1) if found else ""


def _results(parts) -> list:
    """(method, verdict, properties) for each dmarc, dkim and spf result in a header."""
    found_all = []
    # parts[0] is the server's own name, and says nothing about the letter.
    for part in parts[1:]:
        found = _RESULT.match(part)
        if not found:
            continue
        method, verdict = found.groups()
        properties = {}
        for name, value in _PROPERTY.findall(part[found.end():]):
            properties.setdefault(name, value)
        found_all.append((method, verdict, properties))
    return found_all


def _believed(headers: list, trusted: str):
    """
    The results that count, out of every Authentication-Results header of a
    letter, top-most first — or None when they cannot be read.

    With no server named, the top-most header alone: see sender_authenticated.

    With one named, every header written under that name and none written
    under another. That reads a server which spreads its verdict over several
    headers in full, and ignores a header that came with the letter under
    somebody else's name.

    Under the trusted name itself, each method — DKIM, SPF, DMARC — may be
    reported by one header only. Two headers reporting the same one means one
    of them is not the server's, and there is no telling which: it was first
    taken to be the lower, on the reasoning that a server adds its headers
    above whatever arrived, but a filter may as well append them underneath,
    and then the forgery is the one on top — over the server's own dmarc=fail.
    So neither is believed and the letter proves nothing.
    """
    if not trusted:
        parts = _split_results(headers[0])
        return _results(parts) if parts else None

    results, reported = [], set()
    for header in headers:
        parts = _split_results(header)
        if parts is None:
            # A header that does not parse cannot be put under a name, the
            # trusted one included, and what it would have said is unknown.
            return None
        if _authserv_of(header) != trusted:
            continue
        found = _results(parts)
        methods = {method for method, _, _ in found}
        if methods & reported:
            _warn_of_a_second_header(trusted, methods & reported)
            return None
        results += found
        reported |= methods
    return results


def _warn_of_a_second_header(trusted: str, methods: set):
    """Says, once in a while, that the trusted name was found on a header it should not be on."""
    global _repeat_warned_at
    now = time.time()
    if now - _repeat_warned_at < REPEAT_WARNING_EVERY:
        return
    _repeat_warned_at = now
    logger.warning(
        f"A letter carries more than one Authentication-Results header under {trusted!r} "
        f"reporting {', '.join(sorted(methods))}, so its sender was treated as unproven. "
        f"Either one of them arrived with the letter — which means the mail server does not "
        f"remove incoming headers that claim its own name, and a /broadcast by mail could be "
        f"forged — or the server itself reports the same check from two filters, in which "
        f"case /broadcast by mail cannot work with a server named in the settings.")


# Where a line ends, as the standard library's parser sees it: at CR LF, at
# LF — and at a CR by itself. CR LF stands first so that it is taken whole;
# tried the other way round, every CR LF is a bare CR followed by an LF.
_LINE_END = re.compile(rb"\r\n|\r|\n")


def _split_by_a_bare_cr(raw: bytes) -> bool:
    """
    Whether the headers hold a CR that is not the start of CR LF.

    The parser this bot reads a letter with takes a CR alone for the end of a
    line. A mail server does not: to it the CR is a character in the middle of
    one. So `Subject: hi<CR>Authentication-Results: …` is one header, a
    Subject, to the server — and to every filter on it, including the one that
    removes Authentication-Results headers claiming its name — and two headers
    to the bot, the second of them a verdict the sender wrote. The letter
    looked to its server like nothing worth removing anything from.

    A letter whose headers hold such a CR reads differently here from the way
    it was checked, and so what was checked is not known to be what is read.
    """
    previous = 0
    for found in _LINE_END.finditer(raw):
        if found.start() == previous:
            # A line with nothing on it: the headers are over, and what the
            # body holds is nobody's business here.
            return False
        if found.group() == b"\r":
            return True
        previous = found.end()
    return False


def sender_authenticated(msg, from_email: str, raw: bytes = None) -> bool:
    """
    Whether the mailbox's own server vouches for the From address.

    `raw` is the letter as it came off the server, when the caller has it: the
    poll loop does, and passes it, for the sake of _split_by_a_bare_cr.

    Read from Authentication-Results (RFC 8601). Which of those headers are
    the server's own is the whole difficulty: anything the sender put into the
    letter is there too, saying whatever its author liked.

    With no server named in the settings (MAIL_AUTH_SERVER), the top-most one
    alone is read: that is the header our provider wrote last. A server that
    spreads its verdict over several headers is therefore read only as far as
    the first — and a server that adds no header of its own leaves the
    forger's on top. The large providers do neither.

    With a server named, the headers under that name are read, all of them,
    and no others — see _believed. That holds as long as the server removes an
    incoming header that already claims its name, which the standard requires
    of it (section 5) and which is what a mail server of one's own has to be
    checked for.

    Proof is a DMARC verdict of pass for this very From domain. Where the
    server reports no DMARC verdict, or only "none" for a domain that publishes
    no policy, it is a passing DKIM signature or SPF check from the sender's
    own domain — a valid signature from somewhere else proves only that
    somebody signed it. Any DMARC verdict other than pass settles it the other
    way, whatever else the header says.

    No such header means no proof, not a pass, and neither does one that does
    not parse.
    """
    if raw is not None and _split_by_a_bare_cr(raw):
        return False
    headers = [_flat(header) for header in msg.get_all("Authentication-Results") or []]
    sender_domain = _domain((from_email or "").lower())
    if not headers or not sender_domain:
        return False
    trusted = str(getattr(config, "MAIL_AUTH_SERVER", "") or "").strip().lower()
    results = _believed(headers, trusted)
    if not results:
        return False

    dmarc, vouched = [], False
    for method, verdict, properties in results:
        if method == "dmarc":
            if verdict != "none":
                dmarc.append(verdict == "pass"
                             and _domain(properties.get("header.from")) == sender_domain)
        elif verdict == "pass":
            if method == "dkim":
                signer = _domain(properties.get("header.d") or properties.get("header.i"))
            else:
                signer = _domain(properties.get("smtp.mailfrom") or properties.get("smtp.mail"))
            vouched = vouched or _aligned(signer, sender_domain)

    if dmarc:
        return all(dmarc)
    return vouched


# How many of the newest letters are looked at for the name of the server.
# Enough to see whether one name heads them all; the answer is the same for a
# thousand.
AUTH_SCAN_LIMIT = 30
_AUTH_HEADERS = "(BODY.PEEK[HEADER.FIELDS (AUTHENTICATION-RESULTS)])"


def scan_auth_servers(limit: int = AUTH_SCAN_LIMIT) -> dict:
    """
    Under which name the newest letters in the mailbox were vouched for.

    For the settings page, to say what belongs in MAIL_AUTH_SERVER: the name
    in the top-most Authentication-Results header of each letter, which is the
    one the mailbox's own server wrote — if it writes one. Returned as
    {"letters": n, "names": [(name, how many letters), …], "without": n},
    the commonest name first. One name heading every letter is the server's;
    a different one on every letter is a server that adds none, the names
    being whatever each letter arrived with.

    Looks and no more: headers alone, with PEEK, in a folder opened read-only.
    """
    if not config.IMAP_USER or not config.IMAP_PASSWORD:
        raise RuntimeError("the mailbox is not set up")

    counts, letters, without = {}, 0, 0
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_SERVER, config.IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.login(config.IMAP_USER, config.IMAP_PASSWORD)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"the Inbox could not be opened: {status}")

        # By UID, like everything the Mail pages ask: a sequence number means
        # another letter the moment something is moved out from under it, and
        # the clear-out moves letters.
        status, response = mail.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"the mailbox could not be searched: {status}")
        numbers = response[0].split() if response and response[0] else []
        numbers = numbers[-limit:]
        if not numbers:
            return {"letters": 0, "names": [], "without": 0}

        status, data = mail.uid("FETCH", b",".join(numbers).decode(), _AUTH_HEADERS)
        if status != "OK" or not data:
            raise RuntimeError(f"the letters could not be read: {status}")
        for item in data:
            # Every other element is the literal; the rest are the ")" bits.
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            letters += 1
            try:
                raw = item[1].decode("utf-8", errors="ignore")
                found = email.message_from_string(raw).get_all("Authentication-Results") or []
                name = _authserv_of(_flat(found[0])) if found else ""
            except Exception:
                # One unreadable letter is not a reason to abandon the rest.
                name = ""
            if name:
                counts[name] = counts.get(name, 0) + 1
            else:
                without += 1
        names = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return {"letters": letters, "names": names, "without": without}
    finally:
        _disconnect(mail, graceful=True)


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
# Whether the last cycle ended on a letter put off (see TryLater).
_putting_off = False


# --- Clearing out the mailbox ---
# Read letters go to the Trash rather than to nowhere: what a cleanup took can
# still be looked at, and a mistake in a schedule is then a mistake and not a
# loss. The folder is asked of the server rather than guessed, because its name
# depends on the provider and on the language the mailbox was created in.

# RFC 6154 marks the Trash with an attribute; these are the names to fall back
# on when the server does not use it.
TRASH_NAMES = ("Trash", "INBOX.Trash", "Deleted Items", "Deleted Messages",
               "[Gmail]/Trash", "Корзина", "INBOX.Корзина", "Удалённые")
# The same for the folder sent letters are kept in, which the panel reads and
# the copies of the bot's own letters are put into.
SENT_NAMES = ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages", "Sent Mail",
              "[Gmail]/Sent Mail", "Отправленные", "INBOX.Отправленные")

# Letters are moved in batches: a mailbox left alone for a year holds thousands,
# and one command carrying every uid of them is a line no server enjoys.
CLEANUP_BATCH = 200


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def quote_astring(value: str) -> str:
    """
    A string as an IMAP quoted string: backslash and double quote escaped.

    Shared by every command in this project that names a folder, a search term
    or a Message-ID by hand — `mailfolders.py` reuses this rather than keeping
    its own copy, which is what let it and `_move_to_trash` below disagree on
    the escaping until this was written once.
    """
    # A quoted string cannot hold a control character at all, and CR LF is how
    # a command ends: a line break let through here is the end of ours and the
    # start of whatever follows it. The search box of the mail pages is typed
    # into a URL, where a line break is two characters anybody can put in a
    # link — "a%0D%0AZ1 SELECT INBOX%0D%0AZ2 UID FETCH 1:* BODY[]" opened the
    # Inbox for writing and marked every letter the bot had not answered yet
    # as read. Turned into spaces rather than refused: every caller shares
    # this, and none of them has a use for a control character.
    value = _CONTROL.sub(" ", str(value))
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def _folder_name(line) -> str:
    """The mailbox name out of one LIST line, quotes and all removed."""
    text = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
    # ( attributes ) "delimiter" "name" — the name is the tail, and it is the
    # only part that may hold spaces, so splitting from the right is enough.
    parts = text.rsplit('"', 2)
    if len(parts) == 3 and parts[1]:
        return parts[1]
    return text.split()[-1].strip('"')


def decode_folder_name(name: str) -> str:
    """
    A folder name as a person reads it, out of IMAP's modified UTF-7.

    The server never says "Корзина": it says "&BBoEPgRABDcEOAQ9BDA-". A list of
    fallback names compared against the raw form could therefore only ever match
    the Latin ones, which is how the Cyrillic entries above sat there for a
    release doing nothing. Only for comparing and showing — the raw name is
    what a SELECT has to be given back.
    """
    out = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def special_folder(mail, attribute: str, names) -> str:
    """
    The folder carrying an RFC 6154 attribute, or one of the names, or "".

    Asked for by its special-use attribute first: the Trash is called Корзина on
    one provider and [Gmail]/Trash on another, and a list of names would be a
    list of the providers somebody happened to try. The attribute is looked for
    among the attributes only — a folder somebody named "\\Sent" is not one.
    """
    try:
        status, lines = mail.list()
    except Exception as e:
        logger.warning(f"Could not list the mailbox folders: {e}")
        return ""
    if status != "OK" or not lines:
        return ""

    wanted = attribute.lower()
    names_seen = []
    for line in lines or []:
        if line is None:
            continue
        text = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else str(line)
        name = _folder_name(line)
        flags = text[text.find("(") + 1:text.find(")")].lower().split() if "(" in text else []
        if wanted in flags:
            return name
        names_seen.append(name)

    by_name = {}
    for name in names_seen:
        by_name.setdefault(name.lower(), name)
        by_name.setdefault(decode_folder_name(name).lower(), name)
    for candidate in names:
        if candidate.lower() in by_name:
            return by_name[candidate.lower()]
    return ""


def trash_folder(mail):
    """The mailbox's Trash folder, or an empty string when it has none."""
    return special_folder(mail, "\\Trash", TRASH_NAMES)


def sent_folder(mail):
    """The folder sent letters are kept in, or an empty string when it has none."""
    return special_folder(mail, "\\Sent", SENT_NAMES)


def _move_to_trash(mail, uids, folder) -> int:
    """Moves the letters to the folder, by MOVE where the server has it."""
    quoted = quote_astring(folder)
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
    return periodic.due("MAIL_CLEANUP_ENABLED", "MAIL_CLEANUP_DAYS",
                        "MAIL_CLEANUP_LAST_AT", now=now)


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
        # The mailbox answers and the checks succeed, yet nothing is being
        # dealt with: letters are being put off (see TryLater). Without this
        # the dashboard would call such a loop healthy.
        "waiting": _putting_off,
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
    global _consecutive_failures, _last_ok_at, _last_error, _putting_off

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
        put_off = False
        for num in messages:
            msg_id = None
            try:
                # PEEK, or the letter is read the moment it is fetched: RFC822
                # is BODY[] under another name, and fetching BODY[] sets \Seen
                # by itself (RFC 3501, 6.4.5). Everything this loop promises —
                # a letter retried after a failure, one put off until 3x-ui is
                # back — rests on it still being unread on the next cycle, and
                # the clear-out below moves read letters to the Trash.
                status, data = mail.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not data:
                    logger.error(f"Could not fetch letter #{num}")
                    continue
                
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                msg_id = msg.get("Message-ID") or None

                from_email, sender_name = sender_name_from(msg.get("From", ""))
                if not from_email:
                    # Nobody to answer, now or on any later cycle; and since
                    # the fetch no longer marks it read, leaving it would mean
                    # fetching it again for ever.
                    logger.warning(f"Could not extract the sender address for letter #{num}; "
                                   f"marked read.")
                    _mark_seen(mail, num)
                    continue

                # Before anything is read or answered: see automatic_reason and
                # over_limit above. Both mark the letter read — left unread it
                # would be looked at, and refused, on every cycle from now on.
                reason = automatic_reason(msg, from_email)
                if reason:
                    logger.info(f"Letter #{num} from {from_email} was sent by a machine "
                                f"({reason}); marked read and not answered.")
                    _mark_seen(mail, num)
                    continue
                proven = sender_authenticated(msg, from_email, raw=raw_email)
                if over_limit(from_email, proven):
                    logger.warning(f"{from_email} has had {REPLY_LIMIT} answers within "
                                   f"{REPLY_WINDOW // 60} minutes; letter #{num} is marked "
                                   f"read and not answered.")
                    _mark_seen(mail, num)
                    continue

                subject = decode_mime_header(msg.get("Subject", ""))
                body = get_email_body(msg)

                handle(num, from_email, subject, body, mail, sender_name,
                       authenticated=proven)
                note_answered(from_email, proven)
                if msg_id:
                    _attempts.pop(msg_id, None)

            except TryLater as e:
                # Not counted against the letter and not marked read. The rest
                # of the cycle is skipped: what stopped this one stops them all.
                put_off = True
                level = logging.DEBUG if _putting_off else logging.WARNING
                logger.log(level, f"Letter #{num} is put off until the next check: {e}. "
                                  f"It stays unread, and so do the ones after it.")
                break

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

        # Said once when it starts and once when it ends: while 3x-ui is away
        # the same letter is put off every POLL_INTERVAL_SECONDS, and a warning
        # each time would bury the dashboard's list of problems under one fact.
        if _putting_off and not put_off and messages:
            logger.info("Letters that were put off are being handled again.")
        _putting_off = put_off

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
