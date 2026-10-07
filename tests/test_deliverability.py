"""
Reading SPF, DMARC and a server's verdict — with DNS and the mailbox replaced.

What is checked is the reading: which records count as missing, broken or
fine, and how an Authentication-Results header turns into findings.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import deliverability as d


class DnsCase(unittest.TestCase):
    def setUp(self):
        self.saved = (config.SMTP_USER, config.SMTP_SERVER, d._txt)
        config.SMTP_USER = "bot@shop.example"
        config.SMTP_SERVER = "smtp.yandex.ru"
        self.records = {}
        d._txt = lambda name: self.records.get(name, [])

    def tearDown(self):
        config.SMTP_USER, config.SMTP_SERVER, d._txt = self.saved

    def levels(self):
        return [f.level for f in d.check_dns()]

    def test_no_records_at_all(self):
        self.assertEqual(self.levels(), ["bad", "warn"])

    def test_everything_in_order(self):
        self.records = {"shop.example": ["v=spf1 include:_spf.yandex.net -all"],
                        "_dmarc.shop.example": ["v=DMARC1; p=none"]}
        self.assertEqual(self.levels(), ["ok", "ok"])

    def test_an_spf_that_forgets_the_provider(self):
        self.records = {"shop.example": ["v=spf1 include:_spf.google.com ~all"],
                        "_dmarc.shop.example": ["v=DMARC1; p=none"]}
        self.assertIn("warn", self.levels())

    def test_any_include_under_the_providers_domain_counts(self):
        config.SMTP_SERVER = "smtp.gmail.com"
        self.records = {"shop.example": ["v=spf1 include:_netblocks.google.com ~all"],
                        "_dmarc.shop.example": ["v=DMARC1; p=none"]}
        self.assertEqual(self.levels(), ["ok", "ok"])

    def test_two_spf_records_break_spf(self):
        self.records = {"shop.example": ["v=spf1 -all", "v=spf1 include:x ~all"]}
        self.assertEqual(self.levels()[0], "bad")

    def test_plus_all_lets_anybody_send(self):
        self.records = {"shop.example": ["v=spf1 include:_spf.yandex.net +all"]}
        self.assertIn("bad", self.levels())
        self.records = {"shop.example": ["v=spf1 include:_spf.yandex.net ~all"]}
        self.assertNotIn("bad", self.levels())

    def test_a_strict_dmarc_is_explained(self):
        self.records = {"shop.example": ["v=spf1 include:_spf.yandex.net -all"],
                        "_dmarc.shop.example": ["v=DMARC1; p=reject"]}
        self.assertEqual(self.levels(), ["ok", "ok", "info"])

    def test_a_public_mail_service_is_its_own_business(self):
        config.SMTP_USER = "somebody@gmail.com"
        self.assertEqual(self.levels(), ["info"])


class TheVerdict(unittest.TestCase):
    HEADERS = (b"Authentication-Results: mx.yandex.ru; spf=pass smtp.mail=bot@shop.example;"
               b" dkim=pass header.d=shop.example; dmarc=pass\r\nSubject: x\r\n\r\n")

    def test_all_pass(self):
        self.assertEqual([f.level for f in d._judge(d._results(self.HEADERS))],
                         ["ok", "ok", "ok", "ok"])

    def test_a_failed_dkim(self):
        headers = self.HEADERS.replace(b"dkim=pass", b"dkim=fail")
        self.assertIn("bad", [f.level for f in d._judge(d._results(headers))])

    def test_no_dkim_at_all_is_worth_a_warning(self):
        headers = self.HEADERS.replace(b" dkim=pass header.d=shop.example;", b"")
        self.assertIn("warn", [f.level for f in d._judge(d._results(headers))])

    def test_no_verdict_is_said_plainly(self):
        self.assertEqual([f.level for f in d._judge({})], ["info"])


if __name__ == "__main__":
    unittest.main()
