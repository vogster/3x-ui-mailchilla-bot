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
from html.parser import HTMLParser

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


class MailboxNotSetUp(Exception):
    """No IMAP address or password: nothing to connect to."""


class FolderMissing(Exception):
    """The server has no folder of this kind that can be found."""


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
    status, data = mail.select(_quote(name), readonly=True)
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
    unfolded = re.sub(r"\r?\n[ \t]*", " ", raw)
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
    query = (query or "").strip()
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


class _TextOut(HTMLParser):
    """The words of an HTML letter, for a letter that came with no text part."""

    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "blockquote", "section", "article", "header", "footer"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)

    def text(self):
        joined = "".join(self.out)
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    parser = _TextOut()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:
        return ""
    return parser.text()


# Anything that makes the letter reach out: an image, a background, a stylesheet
# or a font from somebody else's server. Each is a tracker as much as a picture.
_REMOTE = re.compile(
    r"""(?:\b(?:src|background|poster|srcset)\s*=\s*["']?\s*(?:https?:)?//)"""
    r"""|(?:url\(\s*["']?\s*(?:https?:)?//)"""
    r"""|(?:<link\b[^>]*\bhref\s*=\s*["']?\s*(?:https?:)?//)"""
    # An SVG <image> reaches out through xlink:href rather than src; a
    # stylesheet can @import a sheet by quoted string alone, with no url().
    r"""|(?:\bxlink:href\s*=\s*["']?\s*(?:https?:)?//)"""
    r"""|(?:@import\s+["']\s*(?:https?:)?//)""",
    re.IGNORECASE)
_CID = re.compile(r"""cid:([^"'\s)>]+)""", re.IGNORECASE)
_REFRESH = re.compile(r"""<meta\b[^>]*http-equiv\s*=\s*["']?\s*refresh[^>]*>""", re.IGNORECASE)
_BASE = re.compile(r"""<base\b[^>]*>""", re.IGNORECASE)
_DOCTYPE = re.compile(r"""^\s*<!doctype[^>]*>""", re.IGNORECASE)


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

    Our <base> comes first, and the first <base> is the one a browser obeys, so
    every link opens in a new tab rather than inside the frame. A refresh is cut
    out altogether: CSP has no say over it.
    """
    body = markup or ""

    def to_data(match):
        key = match.group(1).strip().strip("<>")
        return inline.get(key, match.group(0))

    body = _CID.sub(to_data, body)
    body = _REFRESH.sub("", body)
    body = _BASE.sub("", body)

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
        text = html_to_text(markup)

    inline = {}
    inline_bytes = 0
    attachments = []
    leaves = [p for p in msg.walk() if not p.is_multipart()]
    for index, part in enumerate(leaves):
        if part is html_part or part is text_part:
            continue
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        if cid and ctype.startswith("image/") and disposition != "attachment" \
                and markup and ("cid:" + cid) in markup:
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
        "has_remote": remote,
        "remote_shown": bool(remote and remote_images),
        "attachments": attachments,
    }


def attachment_from(raw: bytes, index: int):
    """(filename, content type, bytes) of one leaf part, or None."""
    msg = email.message_from_bytes(raw or b"", policy=policy.default)
    leaves = [p for p in msg.walk() if not p.is_multipart()]
    if index < 0 or index >= len(leaves):
        return None
    part = leaves[index]
    name = decode_words(part.get_filename() or "") or f"part-{index}"
    return name, part.get_content_type(), part.get_payload(decode=True) or b""


def fetch_raw(account: Account, folder: str, uid: str):
    """(raw bytes, flags) of one letter, or None when there is no such uid."""
    if not str(uid).isdigit():
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
    subject = reply_subject(letter.get("subject", ""))
    body = (body_text or "").rstrip("\n") + "\n\n" + quote_body(letter)
    headers = {}
    if letter.get("message_id"):
        headers["In-Reply-To"] = letter["message_id"]
    references = " ".join(part for part in (letter.get("references", ""),
                                            letter.get("message_id", "")) if part)
    if references:
        headers["References"] = references

    msg = mailer.send_email_via(
        account.smtp_server, account.smtp_port, account.smtp_user, account.smtp_password,
        to_address, subject, body, service_name=service_name, extra_headers=headers,
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
        status, _ = mail.select(quoted, readonly=True)
        can_search = status == "OK"
        if not can_search:
            # Not fatal — the letters still get appended below — but silently
            # skipping the duplicate check would let a transient failure here
            # double every letter of the batch into the Sent folder with
            # nothing in the log to explain why.
            logger.warning(f"Could not open {folder} to check for duplicates ({status}); "
                           f"appending without checking.")

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
            status, _ = mail.append(quoted, "\\Seen",
                                    imaplib.Time2Internaldate(time.time()), raw)
            if status == "OK":
                appended += 1
            else:
                logger.warning(f"The server refused a copy of a sent letter: {status}")
        if appended:
            logger.debug(f"Copied {appended} sent letter(s) to {folder}.")
        graceful = True
        return appended
    finally:
        if own:
            inbox._disconnect(mail, graceful=graceful)
