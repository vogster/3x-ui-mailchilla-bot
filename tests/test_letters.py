"""
Building a letter.

Every letter goes out in two forms, and the welcome letter carries a picture —
which is one more MIME layer, and only when there is a picture to carry.
"""
import unittest

import config
import email_bot
import email_texts
import templates


class BothParts(unittest.TestCase):
    """A letter without a text part fares badly with spam filters, and a reader
    with HTML switched off would not see the subscription link at all."""

    def setUp(self):
        email_texts.load()

    def test_welcome(self):
        mail = templates.get_welcome_email("https://sub.example.com/abc", 30, 100)
        self.assertTrue(mail.html.strip())
        self.assertTrue(mail.text.strip())
        self.assertIn("https://sub.example.com/abc", mail.text)

    def test_status(self):
        mail = templates.get_status_email("a@b.c", True, 10, 20, 0, 0)
        self.assertTrue(mail.html.strip())
        self.assertTrue(mail.text.strip())

    def test_help_and_notice(self):
        for mail in (templates.get_help_email("a@b.c"),
                     templates.get_notice("unknown")):
            self.assertTrue(mail.html.strip())
            self.assertTrue(mail.text.strip())


class UnknownSubstitution(unittest.TestCase):
    def test_a_brace_that_means_nothing_survives(self):
        # An edited text is free to contain braces; it must not bring the letter
        # down or come out mangled.
        self.assertEqual(templates._SafeFormat({"a": 1}).__missing__("zzz"), "{zzz}")


class Emphasis(unittest.TestCase):
    def test_bold_and_breaks_in_html(self):
        out = str(templates._emph("a **b** c\nd"))
        self.assertIn("<b", out)
        self.assertIn("<br>", out)

    def test_markup_in_the_text_is_escaped(self):
        self.assertIn("&lt;script&gt;", str(templates._emph("<script>")))

    def test_plain_drops_the_asterisks(self):
        self.assertEqual(templates._plain("a **b** c"), "a b c")


class QrCode(unittest.TestCase):
    def setUp(self):
        self._was = config.WELCOME_QR_ENABLED
        email_texts.load()

    def tearDown(self):
        config.WELCOME_QR_ENABLED = self._was

    def test_switched_on(self):
        config.WELCOME_QR_ENABLED = True
        mail = templates.get_welcome_email("https://sub.example.com/abc", 30, 100)
        self.assertIn(templates.QR_CID, mail.images or {})
        self.assertIn("cid:" + templates.QR_CID, mail.html)

    def test_switched_off(self):
        config.WELCOME_QR_ENABLED = False
        mail = templates.get_welcome_email("https://sub.example.com/abc", 30, 100)
        self.assertFalse(mail.images)
        self.assertNotIn("cid:", mail.html)

    def test_the_png_is_a_png_with_a_quiet_zone(self):
        import struct
        png = templates.qr_png("https://sub.example.com/abc")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width = struct.unpack(">I", png[16:20])[0]
        # Four modules of quiet zone on each side, as the standard asks.
        self.assertEqual(width % 8, 0)
        self.assertGreater(width, 8 * 8)


class MimeShape(unittest.TestCase):
    def setUp(self):
        email_texts.load()

    def _types(self, msg):
        out = [msg.get_content_type()]
        if msg.is_multipart():
            for part in msg.get_payload():
                out += self._types(part)
        return out

    def test_a_letter_without_pictures_is_plain_alternative(self):
        mail = templates.get_status_email("a@b.c", True, 10, 20, 0, 0)
        msg = email_bot.build_message("to@example.com", "s", mail, smtp_user="from@example.com")
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        self.assertIn("text/plain", self._types(msg))
        self.assertIn("text/html", self._types(msg))

    def test_a_picture_adds_exactly_one_layer(self):
        config.WELCOME_QR_ENABLED = True
        mail = templates.get_welcome_email("https://sub.example.com/abc", 30, 100)
        msg = email_bot.build_message("to@example.com", "s", mail, smtp_user="from@example.com")
        self.assertEqual(msg.get_content_type(), "multipart/related")
        self.assertIn("multipart/alternative", self._types(msg))
        self.assertIn("image/png", self._types(msg))

    def test_the_picture_is_addressed_by_content_id(self):
        config.WELCOME_QR_ENABLED = True
        mail = templates.get_welcome_email("https://sub.example.com/abc", 30, 100)
        msg = email_bot.build_message("to@example.com", "s", mail, smtp_user="from@example.com")
        images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get("Content-ID"), f"<{templates.QR_CID}>")
        self.assertTrue(images[0].get("Content-Disposition", "").startswith("inline"))

    def test_headers_spam_filters_charge_for(self):
        mail = templates.get_status_email("a@b.c", True, 10, 20, 0, 0)
        msg = email_bot.build_message("to@example.com", "s", mail, smtp_user="from@example.com")
        for header in ("Date", "Message-ID", "X-Mailer", "From", "To", "Subject"):
            self.assertTrue(msg.get(header), f"{header} is missing")


if __name__ == "__main__":
    unittest.main()
