"""
Orders, and turning a paid one into days in 3x-ui.

The two things that must never happen are a payment applied twice and a
payment lost, so most of what is here retries, restarts and repeats things.
payments.json lives in a temporary file: PAYMENTS_PATH is built from the
module's own __file__, like tariffs.json, and a test that forgot would write
over a real installation's orders.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import payments
import providers
import purchases
import templates
from providers.base import Invoice, Provider

DAY_MS = 86400 * 1000
NOW_MS = 1_800_000_000_000

TARIFF = {"id": "t1", "name": "Month", "limit_gb": 100, "expire_days": 30,
          "inbound_ids": [1], "price": 300}


class FakeXui:
    def __init__(self, client=None, fail=False):
        self.client = client
        self.fail = fail
        self.updates = []
        self.added = []
        self.resets = []

    def find_client_by_email(self, email):
        return dict(self.client) if self.client else None

    @staticmethod
    def client_key(client):
        return client.get("uuid")

    def update_client(self, key, **kwargs):
        if self.fail:
            return False
        self.updates.append(kwargs)
        return True

    def reset_traffic(self, remark):
        self.resets.append(remark)
        return True

    def add_client(self, **kwargs):
        if self.fail:
            return None, []
        self.added.append(kwargs)
        self.client = {"uuid": "new", "email": kwargs["email"], "subId": "s1",
                       "expiryTime": kwargs["expiry_ms"]}
        return "new", kwargs["inbound_ids"]


class PollingProvider(Provider):
    id = "fake"
    title = "FakePay"

    def __init__(self, answer=None, refuse=False):
        self.answer = answer
        self.refuse = refuse
        self.asked = 0

    def enabled(self):
        return True

    def create(self, order):
        if self.refuse:
            raise RuntimeError("the provider is down")
        return Invoice(url=f"https://pay.example/{order['id']}", ref="ref-" + order["id"])

    def check(self, order):
        self.asked += 1
        return self.answer


class OrdersCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mailchilla-payments-")
        self.real_path = payments.PAYMENTS_PATH
        payments.PAYMENTS_PATH = os.path.join(self.dir, "payments.json")
        self.saved = (payments._orders, payments._broken)
        payments._orders, payments._broken = [], False

        self.real_now = purchases._now_ms
        purchases._now_ms = lambda: NOW_MS

        self.sent = []
        self.real_send = purchases.mailer.send_email_reply
        self.real_push = purchases.mailer.send_gotify_notification
        purchases.mailer.send_email_reply = lambda to, subject, message: self.sent.append((to, subject, message))
        purchases.mailer.send_gotify_notification = lambda title, message: None

        self.real_shared = purchases.get_shared_client
        self.real_all = providers.ALL, providers._BY_ID

    def tearDown(self):
        payments.PAYMENTS_PATH = self.real_path
        payments._orders, payments._broken = self.saved
        purchases._now_ms = self.real_now
        purchases.mailer.send_email_reply = self.real_send
        purchases.mailer.send_gotify_notification = self.real_push
        purchases.get_shared_client = self.real_shared
        providers.ALL, providers._BY_ID = self.real_all

    def use_xui(self, fake):
        purchases.get_shared_client = lambda: fake
        return fake

    def use_providers(self, *items):
        providers.ALL = list(items)
        providers._BY_ID = {p.id: p for p in items}

    def paid_order(self, email="ann@example.com"):
        order = payments.create(email, TARIFF, "manual")
        payments.mark_paid(order["id"], "admin")
        return order["id"]


class TheStore(OrdersCase):
    def test_an_order_survives_a_reload(self):
        order = payments.create("ann@example.com", TARIFF, "manual")
        payments._orders = []
        payments.load()
        self.assertEqual(payments.get(order["id"])["tariff"]["name"], "Month")

    def test_the_order_keeps_the_tariff_as_it_was_offered(self):
        order = payments.create("ann@example.com", dict(TARIFF), "manual")
        self.assertEqual(order["amount"], 300)
        self.assertEqual(order["tariff"]["expire_days"], 30)

    def test_paying_one_link_closes_the_rest_of_the_letter(self):
        offer = payments.new_offer_id()
        a = payments.create("ann@example.com", TARIFF, "manual", offer_id=offer)
        b = payments.create("ann@example.com", TARIFF, "fake", offer_id=offer)
        other = payments.create("ann@example.com", TARIFF, "fake", offer_id="another")
        self.assertTrue(payments.mark_paid(a["id"], "admin"))
        self.assertEqual(payments.get(b["id"])["status"], payments.CANCELLED)
        self.assertEqual(payments.get(other["id"])["status"], payments.PENDING)

    def test_paid_is_recorded_once(self):
        order = payments.create("ann@example.com", TARIFF, "manual")
        self.assertTrue(payments.mark_paid(order["id"], "admin"))
        self.assertFalse(payments.mark_paid(order["id"], "admin"))

    def test_money_for_an_expired_order_still_counts(self):
        order = payments.create("ann@example.com", TARIFF, "manual", hours=1)
        payments.expire_stale(now_ms=order["expires_at"] + 1)
        self.assertEqual(payments.get(order["id"])["status"], payments.EXPIRED)
        self.assertTrue(payments.mark_paid(order["id"], "fake"))

    def test_closed_orders_are_trimmed_and_open_ones_never(self):
        saved = payments.CLOSED_KEPT
        payments.CLOSED_KEPT = 2
        try:
            closed = [payments.create("a@example.com", TARIFF, "manual") for _ in range(4)]
            for o in closed:
                payments.update(o["id"], status=payments.CANCELLED)
            still_open = payments.create("b@example.com", TARIFF, "manual")
            ids = {o["id"] for o in payments.all_orders()}
            self.assertIn(still_open["id"], ids)
            self.assertEqual(len(ids), 3)
        finally:
            payments.CLOSED_KEPT = saved

    def test_an_unreadable_file_stops_the_writes_rather_than_erasing_it(self):
        with open(payments.PAYMENTS_PATH, "w") as f:
            f.write("{ not json")
        payments.load()
        with self.assertRaises(RuntimeError):
            payments.create("ann@example.com", TARIFF, "manual")
        with open(payments.PAYMENTS_PATH) as f:
            self.assertEqual(f.read(), "{ not json")


class ApplyingAPayment(OrdersCase):
    def test_the_term_runs_on_from_a_subscription_still_running(self):
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com",
                                    "expiryTime": NOW_MS + 10 * DAY_MS}))
        self.assertTrue(purchases.apply(self.paid_order()))
        self.assertEqual(xui.updates[0]["expiry_ms"], NOW_MS + 40 * DAY_MS)
        self.assertEqual(xui.updates[0]["group"], "Month")
        self.assertEqual(xui.updates[0]["total_gb"], 100)
        self.assertEqual(xui.resets, ["ann@example.com"])

    def test_an_expired_subscription_counts_from_today(self):
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com",
                                    "expiryTime": NOW_MS - 100 * DAY_MS}))
        purchases.apply(self.paid_order())
        self.assertEqual(xui.updates[0]["expiry_ms"], NOW_MS + 30 * DAY_MS)

    def test_somebody_who_is_not_a_client_becomes_one(self):
        xui = self.use_xui(FakeXui(None))
        order_id = self.paid_order()
        self.assertTrue(purchases.apply(order_id))
        self.assertEqual(xui.added[0]["group"], "Month")
        self.assertEqual(xui.added[0]["expiry_ms"], NOW_MS + 30 * DAY_MS)
        # The receipt carries the new link, since they have none yet.
        self.assertIn("s1", self.sent[0][2].text)

    def test_a_refused_attempt_is_retried_with_the_same_date(self):
        # The bug this guards against: a retry that worked the date out again
        # would add the days a second time on top of the first attempt's.
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com",
                                    "expiryTime": NOW_MS + 10 * DAY_MS}, fail=True))
        order_id = self.paid_order()
        self.assertFalse(purchases.apply(order_id))
        order = payments.get(order_id)
        self.assertEqual(order["status"], payments.PAID)
        self.assertEqual(order["target_expiry"], NOW_MS + 40 * DAY_MS)
        self.assertGreater(order["next_try_at"], NOW_MS)

        # Meanwhile 3x-ui took the first update after all, and the next try
        # sees the client already extended.
        xui.fail = False
        xui.client["expiryTime"] = NOW_MS + 40 * DAY_MS
        self.assertTrue(purchases.apply(order_id))
        self.assertEqual(xui.updates[0]["expiry_ms"], NOW_MS + 40 * DAY_MS)

    def test_an_applied_order_is_not_applied_again(self):
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com", "expiryTime": 0}))
        order_id = self.paid_order()
        purchases.apply(order_id)
        self.assertFalse(purchases.apply(order_id))
        self.assertEqual(len(xui.updates), 1)

    def test_an_unpaid_order_is_not_applied(self):
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com", "expiryTime": 0}))
        order = payments.create("ann@example.com", TARIFF, "manual")
        self.assertFalse(purchases.apply(order["id"]))
        self.assertEqual(xui.updates, [])


class WatchingTheInvoices(OrdersCase):
    def test_a_paid_invoice_is_noticed_and_applied(self):
        provider = PollingProvider(answer=payments.PAID)
        self.use_providers(provider)
        xui = self.use_xui(FakeXui({"uuid": "u", "email": "ann@example.com", "expiryTime": 0}))
        order = payments.create("ann@example.com", TARIFF, "fake")
        purchases.poll()
        self.assertEqual(payments.get(order["id"])["status"], payments.APPLIED)
        self.assertEqual(len(xui.updates), 1)

    def test_an_invoice_is_not_asked_about_on_every_poll(self):
        provider = PollingProvider(answer=payments.PENDING)
        self.use_providers(provider)
        payments.create("ann@example.com", TARIFF, "fake")
        purchases.poll()
        purchases.poll()
        self.assertEqual(provider.asked, 1)

    def test_a_provider_that_cannot_answer_changes_nothing(self):
        provider = PollingProvider(answer=None)
        self.use_providers(provider)
        order = payments.create("ann@example.com", TARIFF, "fake")
        purchases.poll()
        self.assertEqual(payments.get(order["id"])["status"], payments.PENDING)

    def test_nothing_open_means_nothing_asked(self):
        provider = PollingProvider(answer=payments.PAID)
        self.use_providers(provider)
        purchases.run_if_due()
        self.assertEqual(provider.asked, 0)


class TheOffer(OrdersCase):
    def setUp(self):
        super().setUp()
        self.real_for_sale = purchases.tariffs.for_sale
        purchases.tariffs.for_sale = lambda: [dict(TARIFF)]
        self.saved_manual = (config.PAYMENT_MANUAL_ENABLED, config.PAYMENT_MANUAL_DETAILS)
        config.PAYMENT_MANUAL_ENABLED = True
        config.PAYMENT_MANUAL_DETAILS = "SBP +7 900 000-00-00"

    def tearDown(self):
        purchases.tariffs.for_sale = self.real_for_sale
        config.PAYMENT_MANUAL_ENABLED, config.PAYMENT_MANUAL_DETAILS = self.saved_manual
        super().tearDown()

    def test_one_order_per_tariff_and_way_of_paying(self):
        from providers.manual import Manual
        self.use_providers(Manual(), PollingProvider())
        blocks = purchases.build_offer("ann@example.com")
        self.assertEqual(len(blocks[0]["ways"]), 2)
        orders = payments.all_orders()
        self.assertEqual(len({o["offer_id"] for o in orders}), 1)

    def test_a_provider_that_refuses_is_left_out_of_the_letter(self):
        from providers.manual import Manual
        self.use_providers(Manual(), PollingProvider(refuse=True))
        blocks = purchases.build_offer("ann@example.com")
        self.assertEqual([w["provider"] for w in blocks[0]["ways"]], [Manual().title])

    def test_the_letter_carries_the_details_and_the_order_number(self):
        from providers.manual import Manual
        self.use_providers(Manual(), PollingProvider())
        purchases.send_offer("ann@example.com")
        letter = self.sent[0][2]
        order = next(o for o in payments.all_orders() if o["provider"] == "manual")
        for part in (letter.html, letter.text):
            self.assertIn("SBP +7 900 000-00-00", part)
            self.assertIn(order["id"], part)
            self.assertIn("https://pay.example/", part)

    def test_nothing_on_sale_gets_a_refusal_not_an_empty_letter(self):
        self.use_providers()
        purchases.send_offer("ann@example.com")
        self.assertEqual(self.sent[0][1], templates.notice_subject("not_for_sale"))
        self.assertEqual(payments.all_orders(), [])

    def test_paying_by_transfer_needs_the_details(self):
        from providers.manual import Manual
        config.PAYMENT_MANUAL_DETAILS = "  "
        self.assertFalse(Manual().enabled())


if __name__ == "__main__":
    unittest.main()
