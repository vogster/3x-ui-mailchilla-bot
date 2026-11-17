"""
An IMAP server in memory, answering in the shapes imaplib hands back.

The mail pages parse FETCH responses by hand — where FLAGS sits, which element
is the literal — and a stub that returned tidy dicts would test none of that.
This returns what imaplib returns: tuples for the parts carrying a literal, bare
bytes for the closing parenthesis, a status string beside every list.

It also keeps count of the one thing the panel must never do: change a flag.
Every folder opened without readonly, and every body fetched without PEEK, is
written down, and the tests assert both lists stay empty.
"""
import re
import shlex
from datetime import datetime, timezone
from email import message_from_bytes
from email.policy import compat32, default


def _internaldate(when: datetime) -> str:
    return when.strftime("%d-%b-%Y %H:%M:%S %z")


class Letter:
    def __init__(self, uid, raw, flags=(), when=None):
        self.uid = uid
        self.raw = raw
        self.flags = set(flags)
        self.when = when or datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    @property
    def header(self):
        end = self.raw.find(b"\r\n\r\n")
        if end == -1:
            end = self.raw.find(b"\n\n")
            return self.raw[:end + 2] if end != -1 else self.raw
        return self.raw[:end + 4]


class FakeMailbox:
    """The server's state, shared by every connection made to it."""

    def __init__(self, folders=None):
        # name -> (attributes, [Letter])
        self.folders = folders or {
            "INBOX": ("\\HasNoChildren", []),
            "Sent": ("\\HasNoChildren \\Sent", []),
            "Trash": ("\\HasNoChildren \\Trash", []),
        }
        self.writable_selects = []
        self.unpeeked_fetches = []
        # Every flag change asked for: (folder, uid, command, flags).
        self.stored = []
        # Changes when a server renumbers a folder; the numbers kept from
        # before then mean nothing.
        self.uidvalidity = 1
        self.appended = []
        self.connections = 0
        # Folder names on which the next SELECT/SEARCH should fail, for the
        # tests about what happens when the server itself misbehaves rather
        # than when a folder is simply absent.
        self.fail_select = set()
        self.fail_search = set()

    def add(self, folder, raw, flags=(), when=None):
        letters = self.folders[folder][1]
        uid = (max((l.uid for l in letters), default=0) + 1)
        letters.append(Letter(uid, raw, flags, when))
        return uid

    def connect(self, *args, **kwargs):
        """Stands in for imaplib.IMAP4_SSL."""
        self.connections += 1
        return FakeConnection(self)


class FakeConnection:
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self, box: FakeMailbox):
        self.box = box
        self.selected = None
        self.literal = None

    # -- session -----------------------------------------------------------
    def login(self, user, password):
        return "OK", [b"LOGIN completed"]

    def list(self):
        lines = []
        for name, (attributes, _) in self.box.folders.items():
            # Gmail and Yandex quote every name. Dovecot — a server of one's
            # own, most likely — sends a plain one bare, and a reader that
            # worked on the first kind alone took the "/" for the name.
            bare = getattr(self.box, "bare_names", False) and " " not in name
            shown = name if bare else f'"{name}"'
            lines.append(f'({attributes}) "/" {shown}'.encode())
        return "OK", lines

    def select(self, mailbox="INBOX", readonly=False):
        name = mailbox.strip('"')
        if name not in self.box.folders:
            return "NO", [b"no such folder"]
        if name in self.box.fail_select:
            return "NO", [b"temporary failure"]
        if not readonly:
            self.box.writable_selects.append(name)
        self.selected = name
        self.readonly = readonly
        return "OK", [str(len(self.box.folders[name][1])).encode()]

    def response(self, code):
        """What imaplib keeps of the untagged responses to the last SELECT."""
        if code.upper() == "UIDVALIDITY" and self.selected:
            return code, [str(self.box.uidvalidity).encode()]
        return code, [None]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]

    def shutdown(self):
        pass

    # -- commands ------------------------------------------------------------
    def _letters(self):
        return self.box.folders[self.selected][1]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            return self._fetch(args[0], args[1])
        if command == "STORE":
            return self._store(args[0], args[1], args[2])
        raise AssertionError(f"the fake server does not know UID {command}")

    def _store(self, uid_set, command, flags):
        # As a real server has it: a folder that was only examined cannot be
        # written to, whatever is asked.
        if getattr(self, "readonly", True):
            return "NO", [b"the folder was opened read-only"]
        wanted = {int(u) for u in str(uid_set).split(",") if u}
        named = set(re.findall(r"\\\w+", flags or ""))
        for letter in self._letters():
            if letter.uid in wanted:
                if command.upper().startswith("+"):
                    letter.flags |= named
                elif command.upper().startswith("-"):
                    letter.flags -= named
                else:
                    letter.flags = set(named)
                self.box.stored.append((self.selected, letter.uid, command, sorted(named)))
        return "OK", [b""]

    def _search(self, args):
        if self.selected in self.box.fail_search:
            return "NO", [b"temporary failure"]
        tokens = []
        for arg in args:
            if arg is None:
                continue
            tokens.extend(shlex.split(arg) if arg.startswith('"') else [arg])
        literal, self.literal = self.literal, None
        if literal is not None:
            tokens.append(literal.decode("utf-8"))
        if tokens[:2] == ["CHARSET", "UTF-8"]:
            tokens = tokens[2:]

        def header(letter, name):
            msg = message_from_bytes(letter.header, policy=compat32)
            return str(msg.get(name, "")).lower()

        def decoded(letter):
            msg = message_from_bytes(letter.raw, policy=default)
            words = [str(value) for _, value in msg.items()]
            for part in msg.walk():
                if part.get_content_type().startswith("text/"):
                    words.append(part.get_content())
            return " ".join(words).lower()

        def matches(letter, criteria):
            head = criteria[0].upper()
            if head == "ALL":
                return True, criteria[1:]
            if head == "OR":
                left, rest = matches(letter, criteria[1:])
                right, rest = matches(letter, rest)
                return left or right, rest
            if head in ("FROM", "TO", "SUBJECT"):
                return criteria[1].lower() in header(letter, head.capitalize()), criteria[2:]
            if head == "TEXT":
                # A server searches what the letter says, not how it was encoded:
                # a Cyrillic subject is an encoded word in the raw bytes.
                return criteria[1].lower() in decoded(letter), criteria[2:]
            if head == "HEADER":
                return criteria[2].lower() in header(letter, criteria[1]), criteria[3:]
            if head == "SEEN":
                return "\\Seen" in letter.flags, criteria[1:]
            if head == "UID":
                # "n:*" as a real server reads it: * is the highest uid there
                # is, and a range is the same either way round — so 5:* in a
                # folder whose highest is 3 matches letter 3, which callers
                # have to be ready for.
                top = max((l.uid for l in self._letters()), default=0)
                ends = [top if part == "*" else int(part) for part in criteria[1].split(":")]
                return min(ends) <= letter.uid <= max(ends), criteria[2:]
            raise AssertionError(f"the fake server does not know SEARCH {head}")

        found = [str(l.uid).encode() for l in self._letters() if matches(l, tokens)[0]]
        return "OK", [b" ".join(found)]

    def _fetch(self, uid_set, spec):
        wanted = {int(u) for u in str(uid_set).split(",") if u}
        if "BODY[" in spec and "BODY.PEEK[" not in spec:
            self.box.unpeeked_fetches.append(spec)
        data = []
        for seq, letter in enumerate(self._letters(), 1):
            if letter.uid not in wanted:
                continue
            flags = " ".join(sorted(letter.flags))
            if "HEADER.FIELDS" in spec:
                section, literal = "BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE CONTENT-TYPE)]", letter.header
            else:
                section, literal = "BODY[]", letter.raw
            meta = (f'{seq} (UID {letter.uid} FLAGS ({flags}) '
                    f'INTERNALDATE "{_internaldate(letter.when)}" RFC822.SIZE {len(letter.raw)} '
                    f'{section} {{{len(literal)}}}')
            data.append((meta.encode(), literal))
            data.append(b")")
        return "OK", data

    def append(self, mailbox, flags, date_time, message):
        name = mailbox.strip('"')
        uid = self.box.add(name, message, flags=re.findall(r"\\\w+", flags or ""))
        self.box.appended.append((name, message))
        return "OK", [f"[APPENDUID 1 {uid}] APPEND completed".encode()]
