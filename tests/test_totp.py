"""
The six-digit code: the arithmetic, and what is done with a code once it is right.

The arithmetic is checked against the standard's own figures — an
implementation that agrees with itself and with nobody's phone is the usual
way this goes wrong. The rest is about what a code is worth to somebody who
was not meant to have it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import totp

# RFC 6238, appendix B: the secret is the ASCII string "12345678901234567890",
# here in the base32 an app is given it in. The table lists eight digits; six
# is the last six of them.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


class TheArithmetic(unittest.TestCase):
    def test_it_agrees_with_the_standard(self):
        for at, eight in RFC_VECTORS:
            with self.subTest(at=at):
                self.assertEqual(totp.code(RFC_SECRET, at=at), eight[-6:])

    def test_a_secret_typed_with_spaces_and_in_lower_case_is_the_same_secret(self):
        typed = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
        self.assertEqual(totp.code(typed, at=59), totp.code(RFC_SECRET, at=59))

    def test_a_secret_whose_length_needs_padding_still_decodes(self):
        # Ten bytes is sixteen characters, eleven is eighteen — which base32
        # only reads with padding an app never shows.
        self.assertTrue(totp.valid_secret("JBSWY3DPEHPK3PXP"))
        self.assertTrue(totp.valid_secret("JBSWY3DPEHPK3PXPJB"))
        self.assertEqual(len(totp.code("JBSWY3DPEHPK3PXPJB", at=0)), 6)

    def test_a_new_secret_is_long_enough_and_different_every_time(self):
        first, second = totp.new_secret(), totp.new_secret()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertTrue(totp.valid_secret(first))

    def test_what_is_not_a_secret(self):
        for junk in ("", None, "short", "not base32 at all!", "1111111111111111", "ABCDEFGH"):
            with self.subTest(junk=junk):
                self.assertFalse(totp.valid_secret(junk))


class WhichCodesAreRight(unittest.TestCase):
    AT = 1234567890          # 20 seconds into its step

    def test_the_code_for_now(self):
        self.assertIsNotNone(totp.match(RFC_SECRET, totp.code(RFC_SECRET, at=self.AT), at=self.AT))

    def test_the_one_before_and_the_one_after(self):
        # A clock half a minute out, or a code typed at the end of its step.
        for drift in (-totp.STEP, totp.STEP):
            self.assertIsNotNone(
                totp.match(RFC_SECRET, totp.code(RFC_SECRET, at=self.AT + drift), at=self.AT))

    def test_but_no_further(self):
        for drift in (-2 * totp.STEP, 2 * totp.STEP, 3600):
            self.assertIsNone(
                totp.match(RFC_SECRET, totp.code(RFC_SECRET, at=self.AT + drift), at=self.AT))

    def test_a_code_typed_with_a_space_in_the_middle(self):
        code = totp.code(RFC_SECRET, at=self.AT)
        self.assertIsNotNone(totp.match(RFC_SECRET, f" {code[:3]} {code[3:]} ", at=self.AT))

    def test_what_is_not_a_code(self):
        code = totp.code(RFC_SECRET, at=self.AT)
        for junk in ("", None, "12345", code + "0", code[:5] + "x", "١٢٣٤٥٦", "²²²²²²", "-" + code[1:]):
            with self.subTest(junk=junk):
                self.assertIsNone(totp.match(RFC_SECRET, junk, at=self.AT))

    def test_a_secret_that_is_not_one_has_no_codes(self):
        self.assertIsNone(totp.match("not a secret", "123456", at=self.AT))
        self.assertFalse(totp.accept("", "123456", at=self.AT))


class ACodeIsUsedOnce(unittest.TestCase):
    AT = 1234567890

    def setUp(self):
        totp._accepted.clear()
        self.secret = totp.new_secret()

    def tearDown(self):
        totp._accepted.clear()

    def test_right_the_first_time(self):
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=self.AT), at=self.AT))

    def test_and_refused_the_second(self):
        # Whoever read it over a shoulder, or off a proxy's log.
        code = totp.code(self.secret, at=self.AT)
        self.assertTrue(totp.accept(self.secret, code, at=self.AT))
        self.assertFalse(totp.accept(self.secret, code, at=self.AT))
        self.assertFalse(totp.accept(self.secret, code, at=self.AT + 20))

    def test_an_older_code_is_refused_once_a_newer_one_has_been_used(self):
        older = totp.code(self.secret, at=self.AT - totp.STEP)
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=self.AT), at=self.AT))
        self.assertFalse(totp.accept(self.secret, older, at=self.AT))

    def test_the_next_code_is_accepted_in_its_turn(self):
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=self.AT), at=self.AT))
        later = self.AT + totp.STEP
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=later), at=later))

    def test_a_wrong_code_uses_nothing_up(self):
        self.assertFalse(totp.accept(self.secret, "000000" if totp.code(self.secret, at=self.AT) != "000000"
                                     else "111111", at=self.AT))
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=self.AT), at=self.AT))

    def test_a_new_secret_starts_with_nothing_used(self):
        self.assertTrue(totp.accept(self.secret, totp.code(self.secret, at=self.AT), at=self.AT))
        other = totp.new_secret()
        self.assertTrue(totp.accept(other, totp.code(other, at=self.AT), at=self.AT))


class WhatTheQrCodeCarries(unittest.TestCase):
    def test_the_address_an_app_reads(self):
        address = totp.uri("JBSWY3DPEHPK3PXP", "admin", "Aurora VPN")
        self.assertTrue(address.startswith("otpauth://totp/Aurora%20VPN%3Aadmin?"))
        self.assertIn("secret=JBSWY3DPEHPK3PXP", address)
        self.assertIn("issuer=Aurora%20VPN", address)
        self.assertIn("digits=6", address)
        self.assertIn("period=30", address)

    def test_a_name_cannot_add_parameters_of_its_own(self):
        address = totp.uri("JBSWY3DPEHPK3PXP", "admin&secret=AAAA", "Evil?secret=BBBB&x=")
        self.assertEqual(address.count("secret="), 1)
        self.assertEqual(address.count("?"), 1)

    def test_a_colon_in_a_name_does_not_move_the_divide(self):
        # The label is "issuer:account"; a colon inside either half, escaped or
        # not, is where an app would cut it instead.
        address = totp.uri("JBSWY3DPEHPK3PXP", "ad:min", "Aurora: VPN")
        label = address[len("otpauth://totp/"):address.index("?")]
        self.assertEqual(label, "Aurora%20VPN%3Aad%20min")
        self.assertIn("issuer=Aurora%20VPN&", address)

    def test_the_secret_in_fours_for_typing_by_hand(self):
        self.assertEqual(totp.grouped("jbswy3dpehpk3pxp"), "JBSW Y3DP EHPK 3PXP")


if __name__ == "__main__":
    unittest.main()
