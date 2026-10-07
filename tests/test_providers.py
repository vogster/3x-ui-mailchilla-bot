"""
The online ways of paying, with the network replaced.

What is checked is what we send and how we read the answer: the signature
Heleket verifies, the fields Crypto Pay is asked for, the link a YooMoney
client is sent to — and, above all, that nothing counts as paid unless it is.
The shapes of the answers are the ones the providers document; a provider
that changes them will be found by its own check button before by these.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import payments
import providers
import purchases
from providers import cryptopay, heleket, yoomoney

ORDER = {"id": "ABCD2345", "amount": 300, "created_at": 1_800_000_000_000,
         "expires_at": 1_800_000_000_000 + 24 * 3600 * 1000, "provider_ref": "",
         "tariff": {"name": "Month"}}


class Reply:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self.body


class Network:
    """Records every request and answers from a queue."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def post(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return self.replies.pop(0)


class ProviderCase(unittest.TestCase):
    keys = {}

    def setUp(self):
        self.saved = {k: getattr(config, k) for k in self.keys}
        for k, v in self.keys.items():
            setattr(config, k, v)
        self.real_post = {m: m.requests.post for m in (cryptopay, heleket, yoomoney)}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        for module, post in self.real_post.items():
            module.requests.post = post

    def network(self, module, *replies):
        net = Network(*replies)
        module.requests.post = net.post
        return net


class TheCryptoPayInvoice(ProviderCase):
    keys = {"CRYPTOPAY_ENABLED": True, "CRYPTOPAY_TOKEN": "tok", "CRYPTOPAY_TESTNET": False}

    def test_it_is_priced_in_rubles_and_lives_as_long_as_the_order(self):
        net = self.network(cryptopay, Reply({"ok": True, "result": {
            "invoice_id": 77, "bot_invoice_url": "https://t.me/CryptoBot?start=IV77"}}))
        invoice = cryptopay.CryptoPay().create(dict(ORDER))
        url, kwargs = net.sent[0]
        self.assertEqual(url, "https://pay.crypt.bot/api/createInvoice")
        self.assertEqual(kwargs["headers"]["Crypto-Pay-API-Token"], "tok")
        self.assertEqual((kwargs["json"]["currency_type"], kwargs["json"]["fiat"]), ("fiat", "RUB"))
        self.assertEqual(kwargs["json"]["amount"], "300")
        self.assertEqual(kwargs["json"]["expires_in"], 24 * 3600)
        self.assertEqual(invoice, ("https://t.me/CryptoBot?start=IV77", "77"))

    def test_the_test_network_has_its_own_address(self):
        config.CRYPTOPAY_TESTNET = True
        net = self.network(cryptopay, Reply({"ok": True, "result": {"name": "app"}}))
        cryptopay.CryptoPay().probe()
        self.assertTrue(net.sent[0][0].startswith("https://testnet-pay.crypt.bot/api/"))

    def test_the_status_is_read_from_either_shape_of_answer(self):
        order = {**ORDER, "provider_ref": "77"}
        provider = cryptopay.CryptoPay()
        self.network(cryptopay, Reply({"ok": True, "result": {"items": [
            {"invoice_id": 77, "status": "paid"}]}}))
        self.assertEqual(provider.check(order), payments.PAID)
        self.network(cryptopay, Reply({"ok": True, "result": [
            {"invoice_id": 77, "status": "expired"}]}))
        self.assertEqual(provider.check(order), payments.EXPIRED)
        self.network(cryptopay, Reply({"ok": True, "result": {"items": [
            {"invoice_id": 77, "status": "active"}]}}))
        self.assertEqual(provider.check(order), payments.PENDING)

    def test_a_refusal_is_an_error_not_a_link(self):
        self.network(cryptopay, Reply({"ok": False, "error": {"name": "UNAUTHORIZED"}}))
        with self.assertRaises(RuntimeError):
            cryptopay.CryptoPay().create(dict(ORDER))


class TheHeleketInvoice(ProviderCase):
    keys = {"HELEKET_ENABLED": True, "HELEKET_MERCHANT": "m-uuid", "HELEKET_API_KEY": "secret"}

    def test_the_signature_is_over_the_bytes_actually_sent(self):
        net = self.network(heleket, Reply({"state": 0, "result": {
            "uuid": "u-1", "url": "https://pay.heleket.com/pay/u-1"}}))
        invoice = heleket.Heleket().create(dict(ORDER))
        url, kwargs = net.sent[0]
        body = kwargs["data"]
        expected = hashlib.md5(base64.b64encode(body) + b"secret").hexdigest()
        self.assertEqual(kwargs["headers"]["sign"], expected)
        self.assertEqual(kwargs["headers"]["merchant"], "m-uuid")
        sent = json.loads(body)
        self.assertEqual((sent["amount"], sent["currency"], sent["order_id"]), ("300", "RUB", "ABCD2345"))
        self.assertFalse(sent["is_payment_multiple"])
        self.assertEqual(invoice, ("https://pay.heleket.com/pay/u-1", "u-1"))

    def test_the_lifetime_stays_within_what_heleket_accepts(self):
        net = self.network(heleket, Reply({"state": 0, "result": {"uuid": "u", "url": "x"}}))
        heleket.Heleket().create({**ORDER, "expires_at": ORDER["created_at"] + 72 * 3600 * 1000})
        self.assertEqual(json.loads(net.sent[0][1]["data"])["lifetime"], 43200)

    def test_only_a_final_payment_counts(self):
        order = {**ORDER, "provider_ref": "u-1"}
        cases = {"paid": payments.PAID, "paid_over": payments.PAID,
                 "wrong_amount": payments.PENDING, "check": payments.PENDING,
                 "cancel": payments.EXPIRED, "fail": payments.EXPIRED}
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.network(heleket, Reply({"state": 0, "result": {"payment_status": status}}))
                self.assertEqual(heleket.Heleket().check(order), expected)


class TheYooMoneyLink(ProviderCase):
    keys = {"YOOMONEY_ENABLED": True, "YOOMONEY_WALLET": "4100111222333", "YOOMONEY_TOKEN": "tok"}

    def test_the_link_carries_the_order(self):
        invoice = yoomoney.YooMoney().create(dict(ORDER))
        query = parse_qs(urlparse(invoice.url).query)
        self.assertEqual(query["receiver"], ["4100111222333"])
        self.assertEqual(query["sum"], ["300"])
        self.assertEqual(query["label"], ["ABCD2345"])
        self.assertEqual(query["paymentType"], ["AC"])

    def history(self, *operations):
        return self.network(yoomoney, Reply({"operations": list(operations)}))

    def test_the_price_less_the_commission_counts(self):
        self.history({"label": "ABCD2345", "status": "success", "amount": 291.0})
        self.assertEqual(yoomoney.YooMoney().check(dict(ORDER)), payments.PAID)

    def test_an_edited_link_does_not(self):
        # The sum in the address was lowered before paying; the label was kept.
        self.history({"label": "ABCD2345", "status": "success", "amount": 0.97})
        self.assertEqual(yoomoney.YooMoney().check(dict(ORDER)), payments.PENDING)

    def test_a_payment_still_in_progress_does_not(self):
        self.history({"label": "ABCD2345", "status": "in_progress", "amount": 291.0})
        self.assertEqual(yoomoney.YooMoney().check(dict(ORDER)), payments.PENDING)

    def test_a_refused_token_is_an_error(self):
        self.network(yoomoney, Reply({}, status=401))
        with self.assertRaises(RuntimeError):
            yoomoney.YooMoney().check(dict(ORDER))

    def test_it_is_off_without_the_token(self):
        config.YOOMONEY_TOKEN = ""
        self.assertFalse(yoomoney.YooMoney().enabled())


class LatePayments(unittest.TestCase):
    """A YooMoney link outlives the order; money that arrives late still counts."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-providers-")
        self.real_path = payments.PAYMENTS_PATH
        payments.PAYMENTS_PATH = os.path.join(self.dir, "payments.json")
        self.saved = (payments._orders, payments._broken)
        payments._orders, payments._broken = [], False
        self.real_all = providers.ALL, providers._BY_ID
        self.real_now = purchases._now_ms
        self.real_apply = purchases.apply_due
        purchases.apply_due = lambda: None
        self.asked = []

        test = self

        class Lasting(yoomoney.YooMoney):
            def check(self, order):
                test.asked.append(order["id"])
                return payments.PENDING

        class Closing(cryptopay.CryptoPay):
            def check(self, order):
                test.asked.append(order["id"])
                return payments.PENDING

        providers.ALL = [Lasting(), Closing()]
        providers._BY_ID = {p.id: p for p in providers.ALL}

    def tearDown(self):
        payments.PAYMENTS_PATH = self.real_path
        payments._orders, payments._broken = self.saved
        providers.ALL, providers._BY_ID = self.real_all
        purchases._now_ms = self.real_now
        purchases.apply_due = self.real_apply

    def expired(self, provider):
        order = payments.create("ann@example.com", {"id": "t", "name": "Month", "price": 300,
                                                    "limit_gb": 0, "expire_days": 30,
                                                    "inbound_ids": [1]}, provider, hours=1)
        payments.update(order["id"], status=payments.EXPIRED)
        return order

    def test_an_expired_yoomoney_order_is_still_asked_about(self):
        order = self.expired("yoomoney")
        purchases._now_ms = lambda: order["expires_at"] + 86400 * 1000
        purchases.run_if_due()
        self.assertEqual(self.asked, [order["id"]])

    def test_but_not_for_ever(self):
        order = self.expired("yoomoney")
        purchases._now_ms = lambda: order["expires_at"] + 4 * 86400 * 1000
        purchases.run_if_due()
        self.assertEqual(self.asked, [])

    def test_a_provider_that_closes_its_invoice_is_not_asked(self):
        order = self.expired("cryptopay")
        purchases._now_ms = lambda: order["expires_at"] + 3600 * 1000
        purchases.run_if_due()
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
