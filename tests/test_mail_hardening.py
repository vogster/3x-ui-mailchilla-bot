"""
What a letter — or a link — can do to the mail pages.

Every letter shown there was written by a stranger: anybody can mail the bot.
The page that shows it runs signed in, in the same process as the bot, on a
connection to the very mailbox the bot reads its work from. So the questions
here are not whether a letter displays, but what one can make the panel do:
say something to the mail server the panel did not mean, eat the machine's
memory or its time, or carry the administrator's answer somewhere else.

Each test below is a letter or a request that did one of those.
"""
import sandbox  # noqa: F401 — first: it moves the state files before anything loads them

import imaplib
import os
import sys
import time
import unittest
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config
import inbox
import mailer
import mailfolders
from fake_imap import FakeConnection, FakeMailbox

BOT = "bot@example.com"


def letter(frm="Ben <ben@example.com>", subject="Help me", text="It stopped working",
           html=None, images=(), **headers):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, BOT, subject
    msg["Message-ID"] = "<1@example.com>"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
        part = msg.get_payload()[1]
        for cid, data in images:
            part.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>")
    return bytes(msg)


class SearchIsNotACommand(unittest.TestCase):
    """
    The search box is typed into a URL, and a URL can carry a line break. A
    line break inside a quoted string ends the command as far as the server is
    concerned, and what follows is the next command: SELECT the Inbox for
    writing, fetch every body without PEEK — and every letter the bot had not
    answered yet is marked read. It only takes a link.
    """

    def setUp(self):
        self.box = FakeMailbox()
        self.saved = (imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER)
        imaplib.IMAP4_SSL = self.box.connect
        config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER = BOT, "secret", BOT
        self.box.add("INBOX", letter())
        self.commands = []
        self.real_uid = FakeConnection.uid
        outer = self

        def recording(connection, command, *args):
            outer.commands.append((command,) + args)
            return outer.real_uid(connection, command, *args)

        FakeConnection.uid = recording

    def tearDown(self):
        FakeConnection.uid = self.real_uid
        imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER = self.saved
        self.assertEqual(self.box.writable_selects, [])
        self.assertEqual(self.box.unpeeked_fetches, [])

    def sent_to_the_server(self):
        return [str(part) for command in self.commands for part in command]

    def test_a_quoted_string_cannot_hold_a_line_break(self):
        for hostile in ("a\r\nZ1 SELECT INBOX", "a\nZ1 NOOP", "a\rb", "a\x00b", "tab\there"):
            quoted = inbox.quote_astring(hostile)
            self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in quoted), repr(quoted))

    def test_a_query_with_a_line_break_reaches_the_server_as_one_command(self):
        mailfolders.list_letters(mailfolders.BOT, "inbox",
                                 query="a\r\nZ1 SELECT INBOX\r\nZ2 UID FETCH 1:* BODY[]")
        for part in self.sent_to_the_server():
            self.assertNotIn("\r", part)
            self.assertNotIn("\n", part)

    def test_and_what_was_typed_is_still_searched_for(self):
        found = mailfolders.list_letters(mailfolders.BOT, "inbox", query="Help\r\nme")
        self.assertEqual([row["subject"] for row in found["rows"]], ["Help me"])

    def test_a_query_that_is_only_a_line_break_is_no_query(self):
        found = mailfolders.list_letters(mailfolders.BOT, "inbox", query="\r\n")
        self.assertEqual(len(found["rows"]), 1)


class InlinePictures(unittest.TestCase):
    """
    A picture inside a letter is written into the page as a data: address. The
    budget for that counted each picture once, and the page wrote it out once
    for every place the letter named it: fifteen kilobytes of markup naming one
    picture a thousand times came to a hundred and thirty megabytes, in the
    process the bot shares.
    """

    PICTURE = b"\x89PNG" + b"x" * 100_000

    def test_naming_a_picture_a_thousand_times_does_not_multiply_it(self):
        raw = letter(html='<img src="cid:a">' * 1000, images=[("a", self.PICTURE)])
        parsed = mailfolders.parse_letter(raw)
        self.assertLess(len(parsed["html"]), 2 * mailfolders.INLINE_MARKUP_LIMIT)
        self.assertIn("data:image/png;base64,", parsed["html"])

    def test_a_picture_used_twice_is_shown_twice(self):
        # A logo at the top and again in the footer is an ordinary letter.
        raw = letter(html='<img src="cid:a"><p>text</p><img src="cid:a">',
                     images=[("a", b"\x89PNG" + b"x" * 1000)])
        self.assertEqual(mailfolders.parse_letter(raw)["html"].count("data:image/png;base64,"), 2)


class HtmlThatTakesForEver(unittest.TestCase):
    """
    The letter's markup is searched for things to cut out, and a pattern that
    reads "a tag, then anything up to its closing bracket" starts over at every
    tag that never closes. Sixty kilobytes of "<link " took three seconds, a
    megabyte takes the afternoon — with the bot standing still for all of it.
    """

    def quick(self, markup):
        started = time.time()
        parsed = mailfolders.parse_letter(letter(html=markup))
        self.assertLess(time.time() - started, 2.0, "this used to take many seconds")
        return parsed

    def test_a_long_run_of_spaces_after_an_attribute(self):
        # `\s*=\s*["']?\s*` divides the run between its two halves every way
        # there is. The same for url( and the rest.
        spaces = " " * 300_000
        for markup in ("<img src=" + spaces + "x", "<img src =" + spaces + "'x",
                       "<p style='background:url(" + spaces + "x)'>",
                       "<svg><image xlink:href=" + spaces + "x>",
                       "<style>@import " + spaces + "x</style>",
                       "<img src=" + "\u3000" * 300_000 + "x"):
            with self.subTest(markup=markup[:24]):
                self.quick(markup)

    def test_a_letter_that_is_only_markup_is_turned_into_text_quickly(self):
        # No text part, so the text is made out of the markup — by a parser
        # that took forty seconds over forty kilobytes of this.
        for markup in ("<a " * 60_000, "<!--" * 50_000, "<" * 150_000, "<p>" * 60_000):
            with self.subTest(markup=markup[:8]):
                started = time.time()
                mailfolders.html_to_text(markup)
                self.assertLess(time.time() - started, 2.0)

    def test_a_subject_that_only_looks_encoded(self):
        raw = (b"From: who@example.com\r\nSubject: " + b"=?u?q? " * 16_000
               + b"\r\n\r\nhello\r\n")
        started = time.time()
        mailfolders.parse_letter(raw)
        self.assertLess(time.time() - started, 2.0, "this took six seconds")

    def test_tags_that_never_close(self):
        for tag in ("<link ", "<meta ", "<base ", "<meta http-equiv=refresh "):
            with self.subTest(tag=tag):
                self.quick(tag * 20000)

    def test_a_letter_too_large_to_frame_is_shown_as_text(self):
        huge = "<p>" + "word " * (mailfolders.HTML_LIMIT // 5 + 10) + "</p>"
        parsed = self.quick(huge)
        self.assertEqual(parsed["html"], "")
        self.assertTrue(parsed["html_skipped"])
        self.assertIn("It stopped working", parsed["text"])

    def test_an_ordinary_letter_is_framed(self):
        parsed = self.quick("<p>Hello <b>there</b></p>")
        self.assertIn("<b>there</b>", parsed["html"])
        self.assertFalse(parsed["html_skipped"])


class TheTextOfAnHtmlLetter(unittest.TestCase):
    """html_to_text is done by hand now; these are what it has to keep doing."""

    def test_blocks_become_lines_and_the_rest_runs_together(self):
        text = mailfolders.html_to_text("<p>One <b>bold</b> word</p><p>Two</p><div>Three<br>Four</div>")
        self.assertEqual(text.split("\n"), ["One bold word", "", "Two", "", "Three", "Four"])

    def test_what_is_not_text_is_left_out(self):
        text = mailfolders.html_to_text(
            "<head><title>T</title><style>p{color:red}</style></head>"
            "<body><script>alert(1)</script><p>Shown</p><!-- a > comment < here --></body>")
        self.assertEqual(text, "Shown")

    def test_character_references_are_read(self):
        self.assertEqual(mailfolders.html_to_text("<p>Tom &amp; Jerry &lt;3 &#1055;&#1088;</p>"),
                         "Tom & Jerry <3 Пр")

    def test_a_doctype_is_not_text(self):
        self.assertEqual(mailfolders.html_to_text("<!DOCTYPE html><?xml version='1.0'?><p>Hello</p>"),
                         "Hello")

    def test_a_bracket_that_is_not_a_tag_stays(self):
        self.assertEqual(mailfolders.html_to_text("<p>a < b and b > a</p>"), "a < b and b > a")


class WhatALetterMayNotBring(unittest.TestCase):
    """Tags that would make the frame go somewhere by itself."""

    def framed(self, markup, remote=False):
        return mailfolders.parse_letter(letter(html=markup), remote_images=remote)["html"]

    def ours(self, html):
        """The part of the frame that is the letter's, after the head the panel puts first."""
        return html.split("</style>", 1)[1]

    def test_a_refresh_is_not_left_standing_by_a_bracket_in_quotes(self):
        # The old filter stopped at the first ">", which here is inside the
        # value: the tag survived whole.
        html = self.framed('<meta content="0;url=https://evil.example/?x=>" http-equiv="refresh">')
        self.assertNotIn("<meta", self.ours(html).lower())
        self.assertIn("x-meta", self.ours(html))

    def test_nor_by_never_closing(self):
        html = self.framed("<meta http-equiv=refresh content='0;url=//evil.example'" + " " * 5000)
        self.assertNotIn("<meta", self.ours(html).lower())

    def test_the_letters_own_base_does_not_replace_ours(self):
        html = self.framed('<BASE href="https://evil.example/"><a href="x">x</a>')
        self.assertNotIn("<base", self.ours(html).lower())
        self.assertIn('<base target="_blank">', html)

    def test_a_link_tag_waits_for_show_images_like_everything_remote(self):
        # dns-prefetch and preconnect are not the content policy's to stop.
        markup = '<link rel="dns-prefetch" href="//tracker.example"><p>hi</p>'
        self.assertNotIn("<link", self.ours(self.framed(markup)).lower())
        self.assertIn("<link", self.ours(self.framed(markup, remote=True)).lower())
        self.assertTrue(mailfolders.has_remote_content(markup))

    def test_a_frame_of_its_own_is_not_a_frame(self):
        # A second document the page does not reach into, and with srcdoc one
        # that needs no address the content policy could refuse.
        html = self.framed('<iframe srcdoc="&lt;a href=https://evil.example target=_blank '
                           'rel=opener&gt;x&lt;/a&gt;"></iframe><object data="x"></object>'
                           '<embed src="x"><frameset><frame src="x"></frameset>')
        ours = self.ours(html).lower()
        for tag in ("<iframe", "<object", "<embed", "<frame"):
            self.assertNotIn(tag, ours)
        self.assertIn("<x-iframe", ours)

    def test_the_policy_stays_in_the_head_whatever_comes_before_the_doctype(self):
        # A no-break space is whitespace to Python and text to a browser: the
        # doctype after it is not a doctype, the body has begun, and a policy
        # put after it would be in the body — where it is ignored.
        for before in ("\xa0", "\u2028", "\x0b", "\u3000", "\x85"):
            with self.subTest(before=repr(before)):
                html = self.framed(before + '<!DOCTYPE html><img src="https://tracker.example/x">')
                self.assertTrue(html.startswith('<meta charset="utf-8">'), html[:60])
        # A real doctype still comes first, so the letter keeps its own mode.
        html = self.framed('\n <!DOCTYPE html><p>x</p>')
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html><meta charset"))

    def test_the_policy_is_still_the_first_thing_in_the_frame(self):
        html = self.framed('<meta http-equiv="Content-Security-Policy" content="img-src *"><p>x</p>')
        self.assertLess(html.index("default-src 'none'"), html.index("img-src *"))


class DeepNesting(unittest.TestCase):
    def test_a_letter_nested_past_reading_is_refused_politely(self):
        # A part inside a part, two thousand deep: the parser runs out of stack.
        depth = 2000
        raw = "".join(f"Content-Type: multipart/mixed; boundary=b{i}\r\n\r\n--b{i}\r\n"
                      for i in range(depth))
        raw += "Content-Type: text/plain\r\n\r\nhello\r\n"
        raw += "".join(f"--b{i}--\r\n" for i in reversed(range(depth)))
        with self.assertRaises(mailfolders.LetterUnreadable):
            mailfolders.parse_letter(raw.encode())
        with self.assertRaises(mailfolders.LetterUnreadable):
            mailfolders.attachment_from(raw.encode(), 0)


class TheBotsMailboxIsNeverWritten(unittest.TestCase):
    """
    One function in the mail pages writes to a mailbox: it marks a Support
    letter read and answered once a reply has gone. In the bot's mailbox the
    same mark would be a registration lost — unread is how the bot knows what
    it still owes an answer — so the function refuses that account itself,
    whatever its caller thinks it is doing.
    """

    def setUp(self):
        self.box = FakeMailbox()
        self.saved = (imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER)
        imaplib.IMAP4_SSL = self.box.connect
        config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER = BOT, "secret", BOT
        self.uid = self.box.add("INBOX", letter())

    def tearDown(self):
        imaplib.IMAP4_SSL, config.IMAP_USER, config.IMAP_PASSWORD, config.SMTP_USER = self.saved

    def test_it_refuses_the_bots_account(self):
        with self.assertRaises(ValueError):
            mailfolders.mark_answered(mailfolders.BOT, "inbox", str(self.uid))
        self.assertEqual(self.box.writable_selects, [])
        self.assertEqual(self.box.stored, [])
        self.assertEqual(self.box.connections, 0, "it should not even have connected")

    def test_a_uid_that_is_not_a_number_is_not_sent_to_the_server(self):
        class Other:
            key, configured = "support", True
            imap_server, imap_port, imap_user, imap_password = "imap.example.com", 993, "s@example.com", "x"
        for uid in ("1:*", "1 (\\Deleted)", "٣", ""):
            self.assertFalse(mailfolders.mark_answered(Other(), "inbox", uid))
        self.assertEqual(self.box.stored, [])

    def test_it_marks_the_one_letter_and_closes_nothing(self):
        class Other:
            key, configured = "support", True
            imap_server, imap_port, imap_user, imap_password = "imap.example.com", 993, "s@example.com", "x"
        second = self.box.add("INBOX", letter(subject="Another"))
        closed = []
        real_close = FakeConnection.close
        FakeConnection.close = lambda connection: closed.append(1) or real_close(connection)
        try:
            self.assertTrue(mailfolders.mark_answered(Other(), "inbox", str(self.uid)))
        finally:
            FakeConnection.close = real_close
        flags = {l.uid: l.flags for l in self.box.folders["INBOX"][1]}
        self.assertEqual(flags[self.uid], {"\\Answered", "\\Seen"})
        self.assertEqual(flags[second], set())
        # CLOSE on a folder opened for writing expunges what others marked deleted.
        self.assertEqual(closed, [])


class Support:
    """An account, as far as send_reply needs one."""
    key, configured = "support", True
    smtp_server, smtp_port, smtp_user, smtp_password = "smtp.example.com", 465, "support@example.com", "x"


class AReplyIsAPersonWriting(unittest.TestCase):
    """
    Every letter the bot sends says a program wrote it, which is what keeps two
    programs from answering each other for ever. A reply from Support is typed
    by a person, and marked the same way it is what the other side's ticket
    system or autoresponder is told to disregard.
    """

    def setUp(self):
        self.saved = (mailer.send_email_via, mailfolders.save_sent_copy)
        self.sent = []

        def fake_send(server_host, port, user, password, to_email, subject, message,
                      service_name=None, extra_headers=None, automatic=True):
            built = mailer.build_message(to_email, subject, message, smtp_user=user,
                                         service_name=service_name, extra_headers=extra_headers,
                                         automatic=automatic)
            self.sent.append(built)
            return built

        mailer.send_email_via = fake_send
        mailfolders.save_sent_copy = lambda *args, **kwargs: None

    def tearDown(self):
        mailer.send_email_via, mailfolders.save_sent_copy = self.saved

    def reply(self, **changes):
        original = {"subject": "Phone will not connect", "message_id": "<abc@client.example>",
                    "references": "", "when_full": "",
                    "from": [{"name": "Ben", "address": "ben@example.com"}],
                    "text": "Help", "html": ""}
        original.update(changes)
        mailfolders.send_reply(Support(), original, "ben@example.com", "Looking into it.")
        return self.sent[-1]

    def test_it_is_not_marked_as_written_by_a_program(self):
        msg = self.reply()
        self.assertIsNone(msg["Auto-Submitted"])
        self.assertIsNone(msg["X-Auto-Response-Suppress"])
        self.assertEqual(inbox.automatic_reason(msg, "support@example.com"), "")

    def test_it_is_sent_as_the_text_it_is(self):
        # It used to leave as text/html: line breaks gone at the other end, and
        # whatever markup stood in the quoted letter live again.
        msg = self.reply(text="First line\nSecond <b>line</b> <img src=//tracker.example/x.png>")
        parts = [part for part in msg.walk() if not part.is_multipart()]
        self.assertEqual([part.get_content_type() for part in parts], ["text/plain"])
        body = parts[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("Looking into it.\n", body)
        self.assertIn("> First line\n> Second <b>line</b>", body)

    def test_a_letter_of_the_bots_own_keeps_both_parts(self):
        import templates
        msg = mailer.build_message("ben@example.com", "Hello",
                                   templates.Email(html="<p>hi</p>", text="hi"), smtp_user=BOT)
        self.assertEqual([part.get_content_type() for part in msg.walk()
                          if not part.is_multipart()], ["text/plain", "text/html"])

    def test_the_bots_own_letters_still_are(self):
        msg = mailer.build_message("ben@example.com", "Hello", "<p>hi</p>", smtp_user=BOT)
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")

    def test_it_is_threaded_under_what_it_answers(self):
        msg = self.reply(references="<first@client.example>")
        self.assertEqual(msg["In-Reply-To"], "<abc@client.example>")
        self.assertEqual(msg["References"], "<first@client.example> <abc@client.example>")

    def test_a_subject_cannot_bring_headers_of_its_own(self):
        # Encoded words decode to anything, a line break included.
        msg = self.reply(subject="hi\r\nBcc: stranger@evil.example\r\n\r\nand a body")
        self.assertNotIn("\n", msg["Subject"])
        self.assertNotIn("\r", msg["Subject"])
        wire = msg.as_string()
        self.assertIsNone(msg["Bcc"])
        self.assertNotIn("\nBcc:", wire)

    def test_nor_can_the_message_id_or_the_references(self):
        msg = self.reply(message_id="<abc@client.example>\r\nBcc: stranger@evil.example",
                         references="<one@x>\r\nX-Injected: yes <two@x> not-an-id")
        self.assertEqual(msg["In-Reply-To"], "<abc@client.example>")
        self.assertEqual(msg["References"], "<one@x> <two@x> <abc@client.example>")
        self.assertIsNone(msg["Bcc"])
        self.assertIsNone(msg["X-Injected"])
        msg.as_string()   # and it can be put on the wire at all

    def test_nothing_at_all_is_still_a_letter_with_a_body(self):
        for empty in ("", mailer.PlainText("")):
            msg = mailer.build_message("ben@example.com", "Hello", empty, smtp_user=BOT)
            self.assertEqual(len([p for p in msg.walk() if not p.is_multipart()]), 1)

    def test_a_message_id_that_is_not_one_is_left_out(self):
        msg = self.reply(message_id="not an id at all")
        self.assertIsNone(msg["In-Reply-To"])


if __name__ == "__main__":
    unittest.main()
