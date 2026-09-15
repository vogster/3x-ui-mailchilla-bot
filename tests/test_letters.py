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


class TheTariffInTheLetter(unittest.TestCase):
    """
    The name of the tariff, beside the term and the limit it gave.

    It answers the question the numbers do not: somebody who writes back asking
    "which one am I on?" was told in the first letter.
    """

    def setUp(self):
        self.saved = config.MAIL_LANG
        config.MAIL_LANG = "ru"
        email_texts.load()

    def tearDown(self):
        config.MAIL_LANG = self.saved
        email_texts.load()

    def test_it_stands_with_the_term_and_the_limit(self):
        mail = templates.get_welcome_email("https://example.com/sub/abc", 90, 100,
                                           tariff="Standard")
        for part in (mail.html, mail.text):
            self.assertIn("Standard", part)
            self.assertLess(part.index("Standard"), part.index("Срок действия"))

    def test_no_tariff_means_no_line(self):
        # Anybody registered before tariffs existed carries no group, and an
        # empty "Тариф:" says less than no line at all.
        mail = templates.get_welcome_email("https://example.com/sub/abc", 90, 100)
        self.assertNotIn("Тариф", mail.html)
        self.assertNotIn("Тариф", mail.text)

    def test_the_text_part_keeps_its_line_breaks(self):
        # trim_blocks eats the newline after a tag, which once glued two lines
        # of a letter together.
        text = templates.get_welcome_email("https://example.com/sub/abc", 90, 100,
                                           tariff="Standard").text
        self.assertIn("Тариф: Standard\n", text)
        self.assertNotIn("StandardСрок", text)


class SupportAndInstructions(unittest.TestCase):
    """
    The address to write to and the page to read, both optional. An
    installation with neither should promise neither — an empty line inviting
    somebody to write to nobody is worse than no line.
    """

    def setUp(self):
        self.saved = (config.SUPPORT_EMAIL, config.MANUAL_URL,
                      config.WELCOME_MANUAL_ENABLED, config.WELCOME_SUPPORT_ENABLED,
                      config.FOOTER_SUPPORT_ENABLED, config.MAIL_LANG)
        config.MAIL_LANG = "ru"
        config.WELCOME_SUPPORT_ENABLED = True
        config.FOOTER_SUPPORT_ENABLED = True
        email_texts.load()

    def tearDown(self):
        (config.SUPPORT_EMAIL, config.MANUAL_URL,
         config.WELCOME_MANUAL_ENABLED, config.WELCOME_SUPPORT_ENABLED,
         config.FOOTER_SUPPORT_ENABLED, config.MAIL_LANG) = self.saved
        email_texts.load()

    def welcome(self):
        return templates.get_welcome_email("https://example.com/sub/abc", 90, 100)

    def test_the_support_address_reaches_every_letter(self):
        config.SUPPORT_EMAIL = "help@example.com"
        for mail in (self.welcome(),
                     templates.get_status_email("ben@example.com", True, 1, 2, 0, 0),
                     templates.get_notice("unknown", subject="?")):
            self.assertIn("help@example.com", mail.html)
            self.assertIn("help@example.com", mail.text)

    def test_no_address_means_no_line(self):
        config.SUPPORT_EMAIL = ""
        mail = templates.get_status_email("ben@example.com", True, 1, 2, 0, 0)
        self.assertNotIn("Вопросы", mail.html)
        self.assertNotIn("Вопросы", mail.text)

    def test_the_registration_letter_says_it_twice_on_purpose(self):
        # Once in its own words, where somebody is setting things up, and once
        # in the footer every letter carries.
        config.SUPPORT_EMAIL = "help@example.com"
        mail = self.welcome()
        self.assertIn("Напишите нам", mail.html)
        self.assertIn("Вопросы", mail.html)

    def test_the_instructions_are_a_link_in_the_letter(self):
        config.MANUAL_URL = "https://example.com/howto"
        config.WELCOME_MANUAL_ENABLED = True
        mail = self.welcome()
        self.assertIn("https://example.com/howto", mail.html)
        self.assertIn("https://example.com/howto", mail.text)

    def test_the_switch_takes_the_link_away(self):
        config.MANUAL_URL = "https://example.com/howto"
        config.WELCOME_MANUAL_ENABLED = False
        self.assertNotIn("https://example.com/howto", self.welcome().html)

    def test_no_address_means_no_link_whatever_the_switch(self):
        config.MANUAL_URL = ""
        config.WELCOME_MANUAL_ENABLED = True
        mail = self.welcome()
        self.assertNotIn("Как настроить", mail.html)

    def test_each_support_line_has_its_own_switch(self):
        # The footer is a signature; the line in the registration letter is
        # help offered to somebody setting a connection up. An installation may
        # well want one without the other, so one switch each.
        config.SUPPORT_EMAIL = "help@example.com"

        config.WELCOME_SUPPORT_ENABLED = False
        config.FOOTER_SUPPORT_ENABLED = True
        mail = self.welcome()
        self.assertNotIn("Напишите нам", mail.html)
        self.assertNotIn("Напишите нам", mail.text)
        self.assertIn("Вопросы", mail.html)

        config.WELCOME_SUPPORT_ENABLED = True
        config.FOOTER_SUPPORT_ENABLED = False
        mail = self.welcome()
        self.assertIn("Напишите нам", mail.html)
        self.assertNotIn("Вопросы", mail.html)
        self.assertNotIn("Вопросы", mail.text)

    def test_the_footer_switch_reaches_every_letter(self):
        # It is the footer of all of them, not of the registration letter.
        config.SUPPORT_EMAIL = "help@example.com"
        config.FOOTER_SUPPORT_ENABLED = False
        for mail in (templates.get_status_email("ben@example.com", True, 1, 2, 0, 0),
                     templates.get_notice("unknown", subject="?")):
            self.assertNotIn("help@example.com", mail.html)
            self.assertNotIn("help@example.com", mail.text)

    def test_the_substitutions_work_in_any_text(self):
        # {support} and {manual} are offered to every editable text, like
        # {service}, so a rewritten letter can mention either.
        config.SUPPORT_EMAIL = "help@example.com"
        config.MANUAL_URL = "https://example.com/howto"
        self.assertEqual(templates.text("common.support"), "Вопросы: help@example.com")

    def test_the_address_is_a_link_to_write_to(self):
        # A reader who has to select and copy an address has already been given
        # one more chore than they came for.
        config.SUPPORT_EMAIL = "help@example.com"
        html = self.welcome().html
        self.assertIn('href="mailto:help@example.com"', html)
        # Both places it appears: the letter's own line and the footer.
        self.assertEqual(html.count('href="mailto:'), 2)

    def test_the_text_part_keeps_the_plain_address(self):
        # Nothing to press in plain text, and a mailto: in it would read as
        # markup that did not render.
        config.SUPPORT_EMAIL = "help@example.com"
        text = self.welcome().text
        self.assertIn("help@example.com", text)
        self.assertNotIn("mailto:", text)

    def test_the_instructions_come_after_every_way_of_setting_it_up(self):
        # The order somebody actually goes through: try it — the button, the
        # apps, the code, the link to copy — then read about it, then ask.
        config.MANUAL_URL = "https://example.com/howto"
        config.SUPPORT_EMAIL = "help@example.com"
        config.WELCOME_MANUAL_ENABLED = True
        html = self.welcome().html
        manual_at = html.index("https://example.com/howto")
        self.assertLess(html.index("btn-cell"), manual_at)          # the app buttons
        self.assertLess(html.index("welcome.manual_intro" and "sub/abc"), manual_at)
        self.assertLess(manual_at, html.index("mailto:help@example.com"))
