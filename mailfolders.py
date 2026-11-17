"""
The mailbox as the panel shows it: three folders, the letters in them, one
letter opened — and the copy of every sent letter that gives the Sent folder
something to show.

Kept apart from inbox.py, which is the bot's poll loop. The loop acts on what
it reads; this only looks, and the one rule it lives by is that looking changes
nothing. Every folder is opened with EXAMINE (select readonly) and every body
is fetched with BODY.PEEK. The bot takes unread letters as the ones still owed
an answer, so a panel that marked a letter read by opening it would swallow a
registration the moment somebody glanced at it.

Nothing here is stored. Each page is a fresh connection, and 3x-ui's rule
applies to the mailbox too: the server is the source of truth, the panel reads
it back.

The copies of sent letters exist because SMTP keeps none. Some providers file
one in Sent by themselves (Gmail always, Yandex when a setting says so), most
do not, and a Sent folder that holds some letters and not others is worse than
an empty one. So after each send the letter is appended over IMAP — unless the
provider has already filed it, which is checked by Message-ID first.
"""
import base64
import email
import html
import imaplib
import logging
import queue
import re
import threading
import time
from datetime import datetime
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import config
import i18n
import inbox
import mailer

logger = logging.getLogger(__name__)

# The folders the panel offers, in the order of its tabs. The Trash is among
# them because the cleanup moves read letters there, and an old conversation
# would otherwise be out of reach.
FOLDERS = ("inbox", "sent", "trash")

PER_PAGE = 50

# The header fields a list row needs. Content-Type is there for one thing: a
# multipart/mixed letter almost always carries an attachment, and that is worth
# a paperclip in the list without downloading anybody's PDF to find out.
_LIST_FETCH = ("(UID FLAGS INTERNALDATE RFC822.SIZE "
               "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE)])")
_LETTER_FETCH = "(UID FLAGS INTERNALDATE BODY.PEEK[])"

# Pictures a letter carries inside itself (cid:) are drawn from data: URIs,
# because the frame the letter is drawn in cannot fetch from the panel. Past
# this much the rest stay out: a page is not the place for a 30 MB photograph.
INLINE_IMAGES_LIMIT = 5 * 1024 * 1024
# And how much of them may be written into the page. The limit above counts
# each picture once; the page writes it out once for every place the letter
# names it, so without this one a hundred-kilobyte picture named a thousand
# times was a hundred and thirty megabytes of markup — in the process the bot
# shares, where running out of memory takes both down. Twice the pictures'
# own size in base64: room for a logo at the top and again in the footer.
INLINE_MARKUP_LIMIT = 2 * (INLINE_IMAGES_LIMIT * 4 // 3)
# A letter's markup past this is not framed at all: its text is shown instead.
# Every pass over the markup below is linear, but linear in something a
# stranger chose the size of.
HTML_LIMIT = 1_000_000
# And a header past this is read only this far.
HEADER_LIMIT = 4000
# How much markup is turned into text when a letter has no text part of its
# own. The standard library's HTML parser has had inputs it takes quadratic
# time over, in releases this project still runs on.
TEXT_FROM_HTML_LIMIT = 200_000


class MailboxNotSetUp(Exception):
    """No IMAP address or password: nothing to connect to."""


class FolderMissing(Exception):
    """The server has no folder of this kind that can be found."""


class LetterUnreadable(Exception):
    """
    The letter is built in a way the parser gives up on — parts nested inside
    parts until it runs out of stack. Two thousand levels fit in a hundred
    kilobytes, and without this the page was a 500 with a traceback.
    """


class Account:
    """
    One mailbox's credentials, read from config at call time rather than kept
    on the instance — a setting saved from the panel must take effect on the
    very next request, with no restart, the same rule as everywhere else in
    this project.

    There are exactly two: the bot's own (config.IMAP_*/SMTP_*, `prefix=""`)
    and Support (config.SUPPORT_IMAP_*/SUPPORT_SMTP_*). `key` is the URL
    segment and the word queued sent-copies are tagged with; nothing else in
    this module hardcodes which accounts exist.
    """

    def __init__(self, key: str, prefix: str):
        self.key = key
        self.prefix = prefix

    def _cfg(self, name):
        return getattr(config, f"{self.prefix}{name}")

    @property
    def imap_server(self): return self._cfg("IMAP_SERVER")

    @property
    def imap_port(self): return self._cfg("IMAP_PORT")

    @property
    def imap_user(self): return self._cfg("IMAP_USER")

    @property
    def imap_password(self): return self._cfg("IMAP_PASSWORD")

    @property
    def smtp_server(self): return self._cfg("SMTP_SERVER")

    @property
    def smtp_port(self): return self._cfg("SMTP_PORT")

    @property
    def smtp_user(self): return self._cfg("SMTP_USER")

    @property
    def smtp_password(self): return self._cfg("SMTP_PASSWORD")

    @property
    def configured(self) -> bool:
        """Whether there is a mailbox to read at all."""
        return bool(self.imap_user and self.imap_password)

    def own_addresses(self) -> set:
        """The addresses this account writes from, for telling outgoing from incoming."""
        found = set()
        for value in (self.imap_user, self.smtp_user):
            _, address = parseaddr(value or "")
            if address:
                found.add(address.strip().lower())
        return found


BOT = Account("bot", "")
SUPPORT = Account("support", "SUPPORT_")
# Order matters here too: it is the order the Mail page offers the tabs in,
# and Support is left out of that order whenever it has nothing configured —
# see accounts_available().
ACCOUNTS = {account.key: account for account in (BOT, SUPPORT)}


def accounts_available() -> list:
    """The accounts worth a tab on the Mail page: the bot's always, Support
    only once both halves of it are set up."""
    return [a for a in (BOT, SUPPORT) if a.key == "bot" or a.configured]


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------

# inbox.py's own quoting (for the Trash cleanup) and this module's used to
# disagree on what gets escaped; both now go through the one helper.
_quote = inbox.quote_astring


def _connect(account: Account):
    if not account.configured:
        raise MailboxNotSetUp()
    mail = imaplib.IMAP4_SSL(account.imap_server, account.imap_port, timeout=inbox.IMAP_TIMEOUT)
    try:
        mail.login(account.imap_user, account.imap_password)
    except Exception:
        inbox._disconnect(mail, graceful=False)
        raise
    return mail


def _examine(mail, folder: str):
    """
    Opens the folder read-only and returns its raw name.

    readonly is what makes this EXAMINE rather than SELECT, and a server keeps
    its hands off the \\Seen flag of an examined folder even if a fetch without
    PEEK slipped through somewhere. Both guards stay: each is cheap.
    """
    return _open(mail, folder, readonly=True)


def _open(mail, folder: str, readonly: bool):
    """_examine's work, for the one caller that has to write: mark_answered."""
    if folder == "inbox":
        name = "INBOX"
    elif folder == "sent":
        name = inbox.sent_folder(mail)
    elif folder == "trash":
        name = inbox.trash_folder(mail)
    else:
        raise FolderMissing(folder)
    if not name:
        raise FolderMissing(folder)
    status, data = mail.select(_quote(name), readonly=readonly)
    if status != "OK":
        # FolderMissing is deliberately not logged where it is caught (the
        # panel treats a not-yet-set-up mailbox as ordinary) — but this branch
        # can also mean the folder exists and something else went wrong
        # (permissions, a transient fault), which is worth a trail rather than
        # silently reading to the admin as "no such folder".
        logger.warning(f"Could not open {name!r} ({folder}) for the panel: {status} {data!r}")
        raise FolderMissing(folder)
    return name


def _fetch_records(data):
    """
    (meta, literal) pairs out of a FETCH response.

    imaplib hands back tuples for the parts carrying a literal and bare bytes for
    whatever follows it. Servers differ in where they put FLAGS — before the
    literal or after it — so the bytes that follow are glued onto the meta of
    the record they belong to rather than thrown away.
    """
    records = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            records.append([item[0] or b"", item[1] or b""])
        elif isinstance(item, bytes) and records:
            records[-1][0] += b" " + item
    return [(meta.decode("utf-8", errors="ignore"), literal) for meta, literal in records]


_UID = re.compile(r"\bUID (\d+)")
_FLAGS = re.compile(r"\bFLAGS \(([^)]*)\)")
_INTERNALDATE = re.compile(r'\bINTERNALDATE "([^"]+)"')
_SIZE = re.compile(r"\bRFC822\.SIZE (\d+)")


def _meta(meta: str) -> dict:
    uid = _UID.search(meta)
    flags = _FLAGS.search(meta)
    internal = _INTERNALDATE.search(meta)
    size = _SIZE.search(meta)
    when = None
    if internal:
        try:
            when = datetime.strptime(internal.group(1), "%d-%b-%Y %H:%M:%S %z")
        except ValueError:
            when = None
    return {
        "uid": uid.group(1) if uid else "",
        "flags": set((flags.group(1) if flags else "").split()),
        "internal": when,
        "size": int(size.group(1)) if size else 0,
    }


# ---------------------------------------------------------------------------
# Headers and addresses
# ---------------------------------------------------------------------------

def decode_words(value: str) -> str:
    """
    Encoded words back into text.

    make_header rather than inbox.decode_mime_header, which joins every chunk
    with a space: fine for an address, but a subject split into a plain part and
    an encoded one would come out with a gap that is not in it.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return inbox.decode_mime_header(value)


def _raw_header(msg, name: str) -> str:
    """
    The first value of a header exactly as it came, still encoded.

    Read through raw_items() rather than msg[name]: the modern policy parses a
    header the moment it is asked for, and a malformed one would take the whole
    letter down with it. Eight-bit bytes a sender put there unencoded arrive as
    surrogate escapes, which are turned back into text here — left in, they
    would break the page when it is written out.
    """
    wanted = name.lower()
    for key, value in msg.raw_items():
        if key.lower() == wanted:
            value = str(value)
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                value = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            return value
    return ""


def _text_header(msg, name: str) -> str:
    """A header as text, whatever it was encoded with."""
    raw = _raw_header(msg, name)
    if not raw:
        return ""
    # Bounded before it is decoded: the decoding looks for encoded words, and
    # on a header made of a hundred kilobytes of things that begin like one it
    # takes six seconds — for every row of the list the letter is on. No
    # subject, address or message id worth showing is that long.
    unfolded = re.sub(r"\r?\n[ \t]*", " ", raw)[:HEADER_LIMIT]
    return decode_words(unfolded)


def _addresses(msg, name: str) -> list:
    """
    [{name, address}] out of an address header.

    Split into people before anything is decoded: a name that decodes to
    "Doe, John" would otherwise be cut at its comma into two people, neither of
    them with an address.
    """
    raw = _raw_header(msg, name)
    if not raw:
        return []
    out = []
    for person, address in getaddresses([re.sub(r"\r?\n[ \t]*", " ", raw)]):
        address = (address or "").strip()
        person = decode_words(person or "")
        if not address and not person:
            continue
        if person.lower() == address.lower():
            person = ""
        out.append({"name": person, "address": address.lower()})
    return out


def _when(msg, fallback=None):
    """The letter's date as local time: the Date header, else the server's own."""
    stamp = None
    raw = _text_header(msg, "Date")
    if raw:
        try:
            stamp = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            stamp = None
    if stamp is None:
        stamp = fallback
    if stamp is None:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp


def correspondent(sender: list, recipients: list, own: set):
    """
    The other side of a letter: whoever wrote it, unless that was us.

    The Trash holds both directions, and in the Sent folder the sender is always
    the bot — so the question is asked of each letter rather than of its folder.
    Returns (person, outgoing).
    """
    first_from = sender[0] if sender else None
    if first_from and first_from["address"] in own:
        # Outgoing is a fact about who sent it, not about whether a recipient
        # could be read back out of it — a copy with a missing or mangled To
        # header is still ours, and must not read as an unanswered letter from
        # ourselves. person is None rather than us when there is truly nobody
        # to show; every caller already handles that.
        return (recipients[0] if recipients else None), True
    return first_from, False


def format_when(stamp, now=None) -> str:
    """Today's letters by the hour, this year's by the day, older ones in full."""
    if stamp is None:
        return "—"
    now = now or datetime.now(stamp.tzinfo)
    if stamp.date() == now.date():
        return stamp.strftime("%H:%M")
    if stamp.year == now.year:
        return stamp.strftime("%d.%m")
    return stamp.strftime("%d.%m.%Y")


_SIZE_UNITS = ("B", "KB", "MB", "GB")


def format_size(value: int, ceiling: str = "MB") -> str:
    """
    Bytes as the shortest unit that still reads as a number.

    `ceiling` stops the climb early: a letter or an attachment never runs to
    gigabytes, so MB is plenty here. `admin/app.py`'s server-status widget
    wants GB for memory and disk, and calls this with `ceiling="GB"` rather
    than keeping a second copy of the same loop.
    """
    stop = _SIZE_UNITS.index(ceiling)
    value = float(value or 0)
    for index, unit in enumerate(_SIZE_UNITS):
        if value < 1024 or index == stop:
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def _search_criteria(query: str):
    """
    (args, literal) for UID SEARCH.

    ASCII goes as quoted strings against the three headers a person means by
    "find". Anything else has to travel as a literal with CHARSET UTF-8, and
    imaplib carries one literal per command — so it becomes a single TEXT
    search, which also looks through the bodies. Close enough for a name typed
    in Cyrillic, and not worth a second round trip to narrow.
    """
    # Whitespace of every kind becomes one space first. A line break is the
    # one that matters — see inbox.quote_astring for what it did — and the
    # literal below is safe from it by construction, but a query is words, and
    # there is no reason for the two paths to disagree about what was typed.
    query = " ".join((query or "").split())
    if not query:
        return ["ALL"], None
    try:
        query.encode("ascii")
    except UnicodeEncodeError:
        return ["CHARSET", "UTF-8", "TEXT"], query.encode("utf-8")
    quoted = _quote(query)
    return ["OR", "OR", "FROM", quoted, "TO", quoted, "SUBJECT", quoted], None


def parse_list_row(meta: str, header_bytes: bytes, own: set) -> dict:
    """One row of the list, out of a FETCH record. No connection needed."""
    info = _meta(meta)
    msg = email.message_from_bytes(header_bytes or b"", policy=policy.compat32)
    sender = _addresses(msg, "From")
    recipients = _addresses(msg, "To") + _addresses(msg, "Cc")
    person, outgoing = correspondent(sender, recipients, own)
    stamp = _when(msg, info["internal"])
    content_type = _text_header(msg, "Content-Type").lower()
    return {
        "uid": info["uid"],
        "subject": _text_header(msg, "Subject").strip(),
        "person": person or {"name": "", "address": ""},
        "outgoing": outgoing,
        "seen": "\\Seen" in info["flags"],
        "answered": "\\Answered" in info["flags"],
        "attachment": content_type.startswith("multipart/mixed"),
        "when": format_when(stamp),
        "when_full": stamp.strftime("%d.%m.%Y %H:%M") if stamp else "",
        "size": format_size(info["size"]),
    }


def list_letters(account: Account, folder: str, page: int = 1, query: str = "",
                 per_page: int = PER_PAGE) -> dict:
    """
    One page of a folder, newest first.

    Raises MailboxNotSetUp, FolderMissing, or whatever the connection throws;
    the route turns each into words.
    """
    mail = _connect(account)
    graceful = False
    try:
        _examine(mail, folder)
        args, literal = _search_criteria(query)
        if literal is not None:
            mail.literal = literal
        status, response = mail.uid("SEARCH", *args)
        if status != "OK":
            raise RuntimeError(f"the folder could not be searched: {status}")
        uids = response[0].split() if response and response[0] else []
        uids.sort(key=int, reverse=True)

        total = len(uids)
        pages = max(1, -(-total // per_page))
        page = min(max(1, int(page or 1)), pages)
        chunk = uids[(page - 1) * per_page:page * per_page]

        rows = []
        if chunk:
            status, data = mail.uid("FETCH", b",".join(chunk).decode("ascii"), _LIST_FETCH)
            if status != "OK":
                raise RuntimeError(f"the letters could not be fetched: {status}")
            own = account.own_addresses()
            for meta, literal_bytes in _fetch_records(data):
                try:
                    rows.append(parse_list_row(meta, literal_bytes, own))
                except Exception as e:
                    # One unreadable header is not a reason to lose the page.
                    logger.debug(f"Skipped an unreadable letter in the list: {e}")
            # The server answers in its own order, which is not ours.
            rows.sort(key=lambda r: int(r["uid"] or 0), reverse=True)
        graceful = True
        return {"folder": folder, "rows": rows, "total": total, "per_page": per_page,
                "page": page, "pages": pages, "query": query}
    finally:
        inbox._disconnect(mail, graceful=graceful)


# ---------------------------------------------------------------------------
# One letter
# ---------------------------------------------------------------------------

def _part_text(part) -> str:
    """A text part's content, whatever charset it claims."""
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


_BLOCKS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                     "table", "blockquote", "section", "article", "header", "footer"})
_SKIPPED = frozenset({"script", "style", "head", "title"})
# The name straight after the bracket, as HTML has it: "a < b" is not a tag.
_TAG_NAME = re.compile(r"(/?)([a-zA-Z][a-zA-Z0-9]*)")


def html_to_text(markup: str) -> str:
    """
    The words of an HTML letter, for one that came with no text part.

    By hand rather than with html.parser, which this used to be. That parser
    looks ahead for the end of whatever it is in the middle of, and on a
    Python from before its fix a letter that is nothing but "<a " over and over
    costs it forty seconds for forty kilobytes — every time the letter is
    opened. This is one pass: the markup is cut at every "<", and each piece
    is a tag up to its ">" and text after it. It knows far less than a parser
    does, and for turning a letter into something to read and to quote that is
    enough.
    """
    out = []
    skipping = ""          # the element whose content is not text: script, style…
    in_comment = False
    pieces = (markup or "").split("<")
    out.append(pieces[0])
    for piece in pieces[1:]:
        if in_comment:
            closed = piece.find("-->")
            if closed == -1:
                continue
            in_comment = False
            if not skipping:
                out.append(piece[closed + 3:])
            continue
        if piece.startswith("!--"):
            closed = piece.find("-->", 3)
            if closed == -1:
                in_comment = True
            elif not skipping:
                out.append(piece[closed + 3:])
            continue
        end = piece.find(">")
        if end != -1 and piece[:1] in ("!", "?"):
            # A doctype or a processing instruction: neither a tag nor text.
            if not skipping:
                out.append(piece[end + 1:])
            continue
        name = _TAG_NAME.match(piece) if end != -1 else None
        if name is None or name.end() > end:
            # Not a tag: "a < b", or one that never closes. Text, as it stands.
            if not skipping:
                out.append("<" + piece)
            continue
        closing, tag = bool(name.group(1)), name.group(2).lower()
        rest = piece[end + 1:]
        if skipping:
            if closing and tag == skipping:
                skipping = ""
                out.append(rest)
            continue
        if tag in _SKIPPED and not closing:
            skipping = tag
            continue
        if tag in _BLOCKS:
            out.append("\n")
        out.append(rest)

    joined = html.unescape("".join(out))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in joined.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# Anything that makes the letter reach out: an image, a background, a stylesheet
# or a font from somebody else's server. Each is a tracker as much as a picture.
# Each alternative is one run of characters before the address, never two with
# something optional between them. `\\s*=\\s*["']?\\s*` reads the same to a
# person and is quadratic to the engine: on `src=` and a long run of spaces it
# tries every way of dividing the run between the two `\\s*`. Twenty-four
# thousand spaces took four seconds, and the engine holds the interpreter for
# all of them — the bot's poll loop and every other page waited.
_REMOTE = re.compile(
    r"""(?:\b(?:src|background|poster|srcset)\s*=[\s"']*(?:https?:)?//)"""
    r"""|(?:url\([\s"']*(?:https?:)?//)"""
    # Any <link> at all, rather than one whose href can be seen to be remote:
    # "a tag, then anything up to its bracket, then href" starts over at every
    # <link that never closes, and sixty kilobytes of them took three seconds.
    # The tag is taken out of the frame until images are asked for either way
    # (see framed_html), so its mere presence is the honest answer to "does
    # this letter reach out".
    r"""|(?:<link\b)"""
    # An SVG <image> reaches out through xlink:href rather than src; a
    # stylesheet can @import a sheet by quoted string alone, with no url().
    r"""|(?:\bxlink:href\s*=[\s"']*(?:https?:)?//)"""
    r"""|(?:@import[\s"']+(?:https?:)?//)""",
    re.IGNORECASE)
_CID = re.compile(r"""cid:([^"'\s)>]+)""", re.IGNORECASE)
# Tags a letter may not bring, made into tags that mean nothing rather than cut
# out. Cutting needs the end of the tag, and both ways of finding it were
# wrong: "up to the first >" stops inside a quoted value —
# <meta content="0;url=//evil/?x=>" http-equiv=refresh> came through whole —
# and "anything but > , then >" is quadratic on tags that never close. A
# renamed tag has no end to find: <x-meta …> is an unknown element to every
# browser, whatever follows it. Every <meta> goes, not only a refresh: the
# letter's charset is already decoded, and a policy of its own could only
# tighten ours.
#
# Anything that holds a document of its own goes the same way. A frame inside
# the letter is a second document the page's own care does not reach into —
# its links are not the ones given rel="noopener" — and with srcdoc it needs
# no address to load, so the content policy has nothing to refuse.
_DEFANG = re.compile(r"<(meta|base|iframe|frame|frameset|object|embed|applet)\b", re.IGNORECASE)
# The same for <link>, but only until "show images": dns-prefetch and
# preconnect tell the sender the letter was opened and are not the content
# policy's to stop, while a stylesheet is what "show images" is asked for.
_DEFANG_LINK = re.compile(r"<(link)\b", re.IGNORECASE)
# HTML's own whitespace and no other. Python's \s also takes a no-break space,
# a line separator and a dozen more that a browser reads as text: with one of
# those in front, the pattern saw a doctype where the browser saw the body
# already begun, and our head — the content policy with it — was put after it,
# inside the body, where a browser ignores a policy altogether.
_DOCTYPE = re.compile(r"""[\t\n\f\r ]*<!doctype[^>]*>""", re.IGNORECASE)


def has_remote_content(markup: str) -> bool:
    return bool(_REMOTE.search(markup or ""))


def framed_html(markup: str, inline: dict, remote: bool) -> str:
    """
    The letter's HTML as the frame's srcdoc, with its reach cut off.

    The frame is sandboxed without scripts, so nothing in the letter runs. What
    a sandbox does not stop is loading: every remote picture is a request that
    tells the sender the letter was opened, and from where. The policy put in
    front of the letter allows data: and nothing else until somebody presses
    "show images" — a meta CSP cannot be loosened by a later one, so a letter
    carrying its own does not undo it.

    Our <base> comes first, and the letter's own is disarmed along with every
    <meta> it carries — a refresh above all, which CSP has no say over — and,
    until images are asked for, every <link>. See _DEFANG for why they are
    renamed rather than cut out.
    """
    body = markup or ""
    budget = INLINE_MARKUP_LIMIT

    def to_data(match):
        nonlocal budget
        key = match.group(1).strip().strip("<>")
        data = inline.get(key)
        # Past the budget the reference is left as it was and the picture shows
        # broken: see INLINE_MARKUP_LIMIT for what the alternative cost.
        if data is None or len(data) > budget:
            return match.group(0)
        budget -= len(data)
        return data

    body = _DEFANG.sub(r"<x-\1", body)
    if not remote:
        body = _DEFANG_LINK.sub(r"<x-\1", body)
    body = _CID.sub(to_data, body)

    sources = "data: https: http:" if remote else "data:"
    policy_value = (f"default-src 'none'; img-src {sources}; "
                    f"style-src 'unsafe-inline'{' https: http:' if remote else ''}; "
                    f"font-src {sources}; form-action 'none'")
    head = (
        '<meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy_value}">'
        '<meta name="referrer" content="no-referrer">'
        '<base target="_blank">'
        # Most letters paint their own background and assume a white page
        # underneath; a letter that paints none would otherwise be dark text on
        # the panel's dark ground.
        '<style>html{background:#fff;color:#1a1a1a}body{margin:12px}</style>'
    )
    doctype = _DOCTYPE.match(body)
    if doctype:
        return body[:doctype.end()] + head + body[doctype.end():]
    return head + body


def parse_letter(raw: bytes, remote_images: bool = False) -> dict:
    """
    Everything the letter page shows, out of the raw letter. No connection.

    Raises LetterUnreadable for a letter nested past what the parser can walk.
    """
    try:
        return _parse_letter(raw, remote_images)
    except RecursionError:
        raise LetterUnreadable() from None


def _parse_letter(raw: bytes, remote_images: bool = False) -> dict:
    """
    parse_letter's work, without its guard.

    Attachments are numbered by their place among the letter's leaf parts, which
    is stable for the same bytes — the download route parses the letter again
    and takes the part by that number.
    """
    msg = email.message_from_bytes(raw or b"", policy=policy.default)

    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    markup = _part_text(html_part) if html_part is not None else ""
    text = _part_text(text_part) if text_part is not None else ""
    if not text and markup:
        text = html_to_text(markup[:TEXT_FROM_HTML_LIMIT])
    # Too large to frame: the text is shown instead, and the page says why.
    html_skipped = len(markup) > HTML_LIMIT
    if html_skipped:
        markup = ""

    inline = {}
    inline_bytes = 0
    attachments = []
    leaves = [p for p in msg.walk() if not p.is_multipart()]
    # Found once, not looked for again per part: a letter is free to have
    # twenty thousand parts and a megabyte of markup.
    referenced = {found.strip().strip("<>") for found in _CID.findall(markup)}
    for index, part in enumerate(leaves):
        if part is html_part or part is text_part:
            continue
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        if cid and ctype.startswith("image/") and disposition != "attachment" \
                and cid in referenced:
            payload = part.get_payload(decode=True) or b""
            if inline_bytes + len(payload) <= INLINE_IMAGES_LIMIT:
                inline[cid] = f"data:{ctype};base64,{base64.b64encode(payload).decode('ascii')}"
                inline_bytes += len(payload)
                continue
            # Over the budget: falls through to the attachment listing below
            # rather than being dropped outright. The <img cid:...> tag will
            # show broken in the frame, but the picture is still one click
            # away instead of simply gone with no way to get it back.
            attachments.append({
                "index": index,
                "name": decode_words(filename) or f"{cid}.{ctype.split('/', 1)[-1]}",
                "type": ctype,
                "size": format_size(len(payload)),
            })
            continue
        if not filename and disposition != "attachment" and ctype.startswith("text/"):
            # A stray text part with no name — a list footer, the other half of
            # an odd alternative — is part of the body, not a file.
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "index": index,
            "name": decode_words(filename) or f"part-{index}",
            "type": ctype,
            "size": format_size(len(payload)),
        })

    sender = _addresses(msg, "From")
    to = _addresses(msg, "To")
    cc = _addresses(msg, "Cc")
    stamp = _when(msg)
    remote = has_remote_content(markup)
    return {
        "subject": _text_header(msg, "Subject").strip(),
        "from": sender,
        "to": to,
        "cc": cc,
        "reply_to": _addresses(msg, "Reply-To"),
        "when_full": stamp.strftime("%d.%m.%Y %H:%M") if stamp else "",
        "message_id": _text_header(msg, "Message-ID").strip(),
        # Carried along only so a reply can thread itself under this letter —
        # References grows by one Message-ID per hop of a conversation.
        "references": _text_header(msg, "References").strip(),
        "text": text,
        "html": framed_html(markup, inline, remote_images) if markup else "",
        "html_skipped": html_skipped,
        "has_remote": remote,
        "remote_shown": bool(remote and remote_images),
        "attachments": attachments,
    }


def attachment_from(raw: bytes, index: int):
    """(filename, content type, bytes) of one leaf part, or None."""
    try:
        msg = email.message_from_bytes(raw or b"", policy=policy.default)
        leaves = [p for p in msg.walk() if not p.is_multipart()]
    except RecursionError:
        raise LetterUnreadable() from None
    if index < 0 or index >= len(leaves):
        return None
    part = leaves[index]
    name = decode_words(part.get_filename() or "") or f"part-{index}"
    return name, part.get_content_type(), part.get_payload(decode=True) or b""


def fetch_raw(account: Account, folder: str, uid: str):
    """(raw bytes, flags) of one letter, or None when there is no such uid."""
    # ASCII digits and nothing else: isdigit() alone is also true of "٣" and
    # "²", which are not numbers to an IMAP server.
    if not (str(uid).isascii() and str(uid).isdigit()):
        return None
    mail = _connect(account)
    graceful = False
    try:
        _examine(mail, folder)
        status, data = mail.uid("FETCH", str(uid), _LETTER_FETCH)
        if status != "OK":
            raise RuntimeError(f"the letter could not be fetched: {status}")
        records = _fetch_records(data)
        graceful = True
        if not records:
            return None
        meta, raw = records[0]
        return raw, _meta(meta)["flags"]
    finally:
        inbox._disconnect(mail, graceful=graceful)


# ---------------------------------------------------------------------------
# Replying
# ---------------------------------------------------------------------------
# Only the Support account offers this — the bot's own mailbox is view-only,
# on purpose: a person replying from inside the bot's own conversation with
# itself would only confuse whoever reads it next. Nothing here enforces that;
# it is the route's job not to call send_reply for the "bot" account.

_RE_PREFIX = re.compile(r"(?i)^re\s*:\s*")


def reply_subject(original_subject: str) -> str:
    """"Re: <subject>", without piling up a second "Re:" on a reply to a reply."""
    subject = (original_subject or "").strip()
    if _RE_PREFIX.match(subject):
        return subject
    return f"Re: {subject}" if subject else "Re:"


def quote_body(letter: dict) -> str:
    """
    The original letter, quoted the way a mail client does it under a typed
    reply: "On <date>, <who> wrote:" and every line prefixed with "> ".

    Built entirely from what parse_letter already extracted — the plain-text
    part, or html_to_text's rendering of the HTML one — so quoting a letter
    touches neither the network nor the letter's raw bytes again.
    """
    sender = letter["from"][0] if letter.get("from") else None
    who = (sender.get("name") or sender.get("address")) if sender else i18n.t("somebody")
    when = letter.get("when_full") or ""
    header = (i18n.t("On {when}, {who} wrote:", when=when, who=who) if when
             else i18n.t("{who} wrote:", who=who))
    body = letter.get("text") or ""
    quoted = "\n".join(f"> {line}" for line in body.splitlines()) or ">"
    return f"{header}\n{quoted}"


def _uidvalidity(mail) -> str:
    """
    The number a server gives a folder's numbering, as text — "0" when it
    gives none. A different one from last time means every uid kept from then
    points at something else now.
    """
    try:
        _, data = mail.response("UIDVALIDITY")
        value = data[0] if data else None
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        return str(int(value)) if value else "0"
    except Exception:
        return "0"


def arrivals(account: Account, validity: str, after_uid: int, limit: int = 5) -> dict:
    """
    The letters that have arrived in an Inbox since a mark, for support_watch.

    `validity` and `after_uid` are that mark: the folder's UIDVALIDITY and the
    highest uid seen. Returns the mark as it now stands (`validity`, `top`),
    how many letters are new (`count`) and the newest `limit` of them as list
    rows, oldest first.

    A folder met for the first time, or one whose numbering has changed, has
    nothing new in it by definition: the answer is its present state and an
    empty list. Letters the account sent itself are not arrivals and are left
    out, though the mark moves past them.

    Strictly looking, like everything here but mark_answered: the folder is
    examined and only headers are fetched.
    """
    mail = _connect(account)
    graceful = False
    try:
        _examine(mail, "inbox")
        current = _uidvalidity(mail)
        known = bool(validity) and str(validity) == current

        # "n:*" always matches the highest letter there is, even one below n:
        # the range reads the same either way round. Hence the filter after.
        wanted = f"{int(after_uid) + 1}:*" if known else "*"
        status, response = mail.uid("SEARCH", "UID", wanted)
        if status != "OK":
            raise RuntimeError(f"the folder could not be searched: {status}")
        uids = sorted(int(u) for u in (response[0].split() if response and response[0] else []))
        if not known:
            graceful = True
            return {"validity": current, "top": uids[-1] if uids else 0, "count": 0, "letters": []}

        uids = [u for u in uids if u > int(after_uid)]
        rows = []
        if uids:
            status, data = mail.uid("FETCH", ",".join(str(u) for u in uids[-limit:]), _LIST_FETCH)
            if status != "OK":
                raise RuntimeError(f"the letters could not be fetched: {status}")
            own = account.own_addresses()
            for meta, literal_bytes in _fetch_records(data):
                try:
                    rows.append(parse_list_row(meta, literal_bytes, own))
                except Exception as e:
                    logger.debug(f"Skipped an unreadable letter among the arrivals: {e}")
        graceful = True
        sent_by_us = sum(1 for row in rows if row["outgoing"])
        rows = sorted((row for row in rows if not row["outgoing"]),
                      key=lambda row: int(row["uid"] or 0))
        return {"validity": current, "top": uids[-1] if uids else int(after_uid),
                "count": len(uids) - sent_by_us, "letters": rows}
    finally:
        inbox._disconnect(mail, graceful=graceful)


def mark_answered(account: Account, folder: str, uid: str) -> bool:
    """
    Marks a letter read and answered, once a reply to it has gone out.

    The one place the panel changes anything in a mailbox, and only ever in
    Support's. Everything else here is strictly looking, and in the bot's
    mailbox that is not a courtesy but how the bot works: unread is the bot's
    own record of a letter it still owes an answer, so nothing may touch it but
    the bot. Support's mailbox has no such reader. There the same strictness
    only meant that a letter answered from the panel went on looking exactly
    like one nobody had got to — and a list that cannot tell the two apart is
    how a letter gets answered twice, or not at all.

    Refuses the bot's account outright rather than trusting its callers to.
    Returns False when the server would not do it, which costs nothing but the
    mark: the reply has already gone.
    """
    if account.key == BOT.key:
        raise ValueError("the bot's mailbox is never written to from the panel")
    if not (str(uid).isascii() and str(uid).isdigit()):
        return False
    mail = _connect(account)
    try:
        _open(mail, folder, readonly=False)
        status, _ = mail.uid("STORE", str(uid), "+FLAGS", "(\\Answered \\Seen)")
        return status == "OK"
    finally:
        # Logged out without closing the folder first. On a folder opened for
        # writing, CLOSE also removes every letter somebody else has marked
        # for deletion and not yet expunged — a decision that is theirs.
        try:
            mail.logout()
        except Exception:
            pass


_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


def _one_line(value) -> str:
    """A header value with every run of whitespace, line breaks included, made one space."""
    return " ".join(inbox._CONTROL.sub(" ", str(value or "")).split())


def send_reply(account: Account, letter: dict, to_address: str, body_text: str,
               service_name: str = None):
    """
    Sends a plain-text reply to a letter and, like every other letter this
    project sends, files a copy of it once it is gone.

    Threaded under the original by In-Reply-To and References, the two
    headers a mail client reads to place a reply in the same conversation
    rather than opening a new one, with the typed text above a quoted copy of
    what it answers — the shape a reply usually takes.
    """
    if not account.configured:
        raise MailboxNotSetUp()
    # Everything that goes into a header here came out of somebody else's
    # letter, decoded — and an encoded word decodes to anything, a line break
    # included. "hi\r\nBcc: …" for a subject is a header of the stranger's own
    # in a letter sent from Support's address, on any Python older than the
    # ones that refuse to write such a header; on those that refuse, it is a
    # letter that cannot be answered from the panel at all. So a subject is
    # one line, and the two threading headers are message ids and nothing
    # else.
    subject = reply_subject(_one_line(letter.get("subject", "")))
    to_address = parseaddr(_one_line(to_address))[1]
    body = (body_text or "").rstrip("\n") + "\n\n" + quote_body(letter)
    headers = {}
    answered = _MESSAGE_ID.findall(_one_line(letter.get("message_id", "")))[:1]
    if answered:
        headers["In-Reply-To"] = answered[0]
    # The last few are enough for a mail client to find the conversation by,
    # and a chain is otherwise as long as its author cares to make it.
    references = (_MESSAGE_ID.findall(_one_line(letter.get("references", ""))) + answered)[-20:]
    if references:
        headers["References"] = " ".join(references)

    msg = mailer.send_email_via(
        account.smtp_server, account.smtp_port, account.smtp_user, account.smtp_password,
        to_address, subject, mailer.PlainText(body), service_name=service_name,
        extra_headers=headers,
        # Typed by a person; see mailer.build_message.
        automatic=False,
    )
    save_sent_copy(account.key, msg.get("Message-ID", ""), msg.as_string().encode("utf-8"))


# ---------------------------------------------------------------------------
# Copies of sent letters
# ---------------------------------------------------------------------------

# How long the worker waits before it connects. Long enough for a provider that
# files its own copy to have done it — or the check by Message-ID would miss it
# and the letter would sit in Sent twice — and for a broadcast to pile up into
# one connection rather than a connection per letter.
COPY_DELAY = 5
# A broadcast to a large list with the mailbox unreachable should not hold every
# letter of it in memory; past this many waiting copies the newest are dropped.
COPY_QUEUE_LIMIT = 2000

_copies = queue.Queue(maxsize=COPY_QUEUE_LIMIT)
_worker = None
_worker_lock = threading.Lock()


def save_sent_copy(account_key: str, message_id: str, raw: bytes):
    """
    Queues a letter that has just gone out for that account's Sent folder.

    Returns at once: the send has already happened, and whatever befalls the
    copy — an unreachable mailbox, a server with no Sent folder — must not cost
    the send anything, nor slow a broadcast down to IMAP's pace.
    """
    account = ACCOUNTS.get(account_key)
    if not account or not account.configured or not raw:
        return
    try:
        _copies.put_nowait((account_key, message_id or "", raw))
    except queue.Full:
        logger.warning("Too many sent letters are waiting to be copied to the Sent folder; "
                       "this one will not be.")
        return
    _ensure_worker()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="sent-copies", daemon=True)
            _worker.start()


def _work():
    while True:
        first = _copies.get()
        time.sleep(COPY_DELAY)
        batch = [first]
        while True:
            try:
                batch.append(_copies.get_nowait())
            except queue.Empty:
                break
        # A batch drained in one pass can hold letters from both accounts —
        # the bot's own and Support's, queued moments apart — and each goes
        # into its own Sent folder, so they are grouped before appending.
        by_account = {}
        for account_key, message_id, raw in batch:
            by_account.setdefault(account_key, []).append((message_id, raw))
        for account_key, items in by_account.items():
            account = ACCOUNTS.get(account_key)
            if not account:
                continue
            try:
                append_copies(account, items)
            except Exception as e:
                logger.warning(f"Could not copy {len(items)} sent letter(s) to {account_key}'s "
                               f"Sent folder: {e}")


def _said(status, data) -> str:
    """
    A refusal in the server's own words, for the log.

    The status alone is "NO", which names neither the folder nor the reason:
    two warnings reading just that were all there was to go on when the Sent
    folder had been taken to be called "/".
    """
    words = ""
    for part in data or []:
        if isinstance(part, bytes):
            words = part.decode("utf-8", errors="replace").strip()
        elif part is not None:
            words = str(part).strip()
        if words:
            break
    return f"{status} {words}".strip()


def append_copies(account: Account, batch, mail=None) -> int:
    """
    Puts the letters into the account's Sent folder. Returns how many were
    appended.

    A letter the provider has already filed — found by its Message-ID — is left
    alone. `mail` is for the tests; the worker opens its own connection.
    """
    own = mail is None
    if own:
        mail = _connect(account)
    graceful = False
    try:
        folder = inbox.sent_folder(mail)
        if not folder:
            logger.warning("The mailbox has no Sent folder that can be found, so copies of "
                           "the letters sent are not kept.")
            graceful = True
            return 0
        quoted = _quote(folder)
        status, said = mail.select(quoted, readonly=True)
        can_search = status == "OK"
        if not can_search:
            # Not fatal — the letters still get appended below — but silently
            # skipping the duplicate check would let a transient failure here
            # double every letter of the batch into the Sent folder with
            # nothing in the log to explain why.
            logger.warning(f"Could not open the Sent folder {folder!r} to check for duplicates "
                           f"({_said(status, said)}); appending without checking.")

        appended = 0
        for message_id, raw in batch:
            if can_search and message_id:
                status, found = mail.uid("SEARCH", "HEADER", "Message-ID", _quote(message_id))
                if status == "OK":
                    if found and found[0] and found[0].split():
                        continue
                else:
                    # Same reasoning as the SELECT above: "could not tell" is
                    # not "not a duplicate", but there is nothing better to do
                    # than append and say why the check was skipped.
                    logger.warning(f"Could not search {folder} for a duplicate of {message_id!r} "
                                   f"({status}); appending without checking.")
            status, said = mail.append(quoted, "\\Seen",
                                       imaplib.Time2Internaldate(time.time()), raw)
            if status == "OK":
                appended += 1
            else:
                logger.warning(f"The server refused a copy of a sent letter in the Sent "
                               f"folder {folder!r}: {_said(status, said)}")
        if appended:
            logger.debug(f"Copied {appended} sent letter(s) to {folder}.")
        graceful = True
        return appended
    finally:
        if own:
            inbox._disconnect(mail, graceful=graceful)
