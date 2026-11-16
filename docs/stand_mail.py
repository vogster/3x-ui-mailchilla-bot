"""
The test stand's mail: two mailboxes kept in memory, and no way out.

The panel has Mail pages now — the bot's own mailbox and Support's — and they
read over IMAP. On a stand that is either a real mailbox, which a stand must
never touch, or this: the in-memory server the tests already use
(tests/fake_imap.py, which answers in the exact shapes imaplib hands back), put
behind imaplib for the whole process and filled with a few letters worth
looking at.

Sending is closed off at the same place. docs/stand.py replaces the function
that sends with one that writes the letter into the outbox; this module makes
sure that anything which reached for SMTP by another road finds a refusal
there rather than a server.
"""
import imaplib
import os
import sys
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import config
import mailer
from fake_imap import FakeMailbox

BOT_HOST, BOT_ADDRESS = "imap.example.com", "bot@example.com"
SUPPORT_HOST, SUPPORT_ADDRESS = "imap.support.example.com", "support@example.com"

# What is set while the stand runs, and put back by the tests afterwards.
SETTINGS = {
    "IMAP_SERVER": BOT_HOST, "IMAP_USER": BOT_ADDRESS, "IMAP_PASSWORD": "x",
    "SMTP_SERVER": "smtp.example.com", "SMTP_USER": BOT_ADDRESS, "SMTP_PASSWORD": "x",
    "SUPPORT_IMAP_SERVER": SUPPORT_HOST, "SUPPORT_IMAP_USER": SUPPORT_ADDRESS,
    "SUPPORT_IMAP_PASSWORD": "x",
    "SUPPORT_SMTP_SERVER": "smtp.support.example.com", "SUPPORT_SMTP_USER": SUPPORT_ADDRESS,
    "SUPPORT_SMTP_PASSWORD": "x",
}


# The name the stand's "mail server" signs its Authentication-Results with.
MAIL_SERVER = "mail.aurora.example"


def _letter(sender, to, subject, text, html=None, **headers) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return bytes(msg)


class Mailboxes:
    """The bot's mailbox and Support's, each a server of its own."""

    def __init__(self):
        self.bot = FakeMailbox()
        self.support = FakeMailbox()
        self._fill()

    def _fill(self):
        # The bot's: one letter it has dealt with and one it has not — unread
        # is how the bot knows it still owes an answer, and the page must show
        # the difference without changing it.
        #
        # Each carries the header a mail server of one's own would have added,
        # in the pieces such a server gives its verdict in, so that the
        # settings page's "Look in the mailbox" has a name to find. The second
        # also carries one the sender brought, underneath — which is what the
        # button must not offer as the server's.
        self.bot.add("INBOX", _letter(
            "Ben <ben@example.com>", BOT_ADDRESS, "/status", "",
            Authentication_Results=f"{MAIL_SERVER}; dkim=pass header.d=example.com"),
            flags=["\\Seen"])
        stranger = EmailMessage()
        stranger["Authentication-Results"] = f"{MAIL_SERVER}; dmarc=none header.from=example.org"
        stranger["Authentication-Results"] = f"{MAIL_SERVER}; dkim=none"
        stranger["Authentication-Results"] = "mx.elsewhere.example; dmarc=pass header.from=example.org"
        stranger["From"], stranger["To"] = "Stranger <who@example.org>", BOT_ADDRESS
        stranger["Subject"] = "Is anybody there?"
        stranger.set_content("I was told to write here.")
        self.bot.add("INBOX", bytes(stranger))
        # Support's: a client with an ordinary question; a letter whose answer
        # is about to go to somebody other than the person on the card; and one
        # that reaches out for a picture, which stays blocked until asked for.
        self.support.add("INBOX", _letter(
            "Anna Bright <anna.bright@example.com>", SUPPORT_ADDRESS,
            "My new phone will not connect",
            "Hello,\n\nI bought a new phone and the app says the subscription is full.\n"
            "What do I do?\n\nAnna"))
        self.support.add("INBOX", _letter(
            "Ben <ben@example.com>", SUPPORT_ADDRESS, "Please send my link again",
            "I lost the letter with the link.",
            Reply_To='"Ben <ben@example.com>" <somebody-else@example.net>'))
        # Everything a letter has been caught trying, in one place: a doctype
        # behind a no-break space, a refresh, a frame of its own, an SVG link
        # and an ordinary one that both ask for a way back. Open it and nothing
        # happens — which is what there is to see.
        self.support.add("INBOX", _letter(
            "Somebody <somebody@example.org>", SUPPORT_ADDRESS, "A letter that tries things",
            "This letter's formatted version tries a number of things.",
            html='\xa0<!DOCTYPE html>'
                 '<meta http-equiv="refresh" content="0;url=https://example.org/?x=>">'
                 '<base href="https://example.org/">'
                 '<h2>Nothing should happen</h2>'
                 '<p><a href="https://example.org/" rel="opener">An ordinary link</a></p>'
                 '<svg width="160" height="30"><a xlink:href="https://example.org/" rel="opener">'
                 '<text x="0" y="20">An SVG link</text></a></svg>'
                 '<iframe srcdoc="&lt;a href=https://example.org rel=opener target=_blank&gt;'
                 'a link in a frame&lt;/a&gt;"></iframe>'
                 '<img src="https://example.org/pixel.png" width="1" height="1">'))
        self.support.add("INBOX", _letter(
            "News <news@example.org>", SUPPORT_ADDRESS, "A letter with a picture in it",
            "A formatted letter; this is its text.",
            html='<h2>Hello</h2><p>This letter loads a picture from somewhere else.</p>'
                 '<img src="https://example.org/pixel.png" width="1" height="1">'
                 '<p><a href="https://example.org/" rel="opener">A link</a></p>'))

    def connect(self, host="", *args, **kwargs):
        """Stands in for imaplib.IMAP4_SSL: which mailbox, by the server asked for."""
        return (self.support if host == SUPPORT_HOST else self.bot).connect()

    def received_by_the_bot(self, sender: str, name: str, subject: str, body: str):
        """
        Files a letter handed to the bot through /stand in the bot's Inbox,
        read — the handler has just dealt with it — so that the Mail page shows
        the same correspondence the outbox does.
        """
        frm = f"{name} <{sender}>" if name else sender
        return self.bot.add("INBOX", _letter(frm, BOT_ADDRESS, subject, body), flags=["\\Seen"])


def _no_smtp(server_host, port, timeout=10):
    raise RuntimeError(f"the stand sends nothing: something tried to reach {server_host}:{port}")


def install(mailboxes: Mailboxes) -> dict:
    """
    Puts the in-memory mailboxes behind imaplib and closes SMTP. Returns what
    was replaced, so that a test can put it back.
    """
    replaced = {"imap": imaplib.IMAP4_SSL, "smtp": mailer.open_smtp,
                "config": {key: getattr(config, key) for key in SETTINGS}}
    imaplib.IMAP4_SSL = mailboxes.connect
    mailer.open_smtp = _no_smtp
    for key, value in SETTINGS.items():
        setattr(config, key, value)
    return replaced


def uninstall(replaced: dict):
    imaplib.IMAP4_SSL = replaced["imap"]
    mailer.open_smtp = replaced["smtp"]
    for key, value in replaced["config"].items():
        setattr(config, key, value)
