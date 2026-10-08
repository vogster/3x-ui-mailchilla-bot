"""The figures on the Analytics page, from data made up here."""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analytics

DAY = 86400 * 1000
NOW = int(datetime(2026, 10, 15, 12, 0).timestamp() * 1000)


def order(email, amount, days_ago, **over):
    base = {"email": email, "amount": amount, "applied_at": NOW - days_ago * DAY,
            "created_at": NOW - days_ago * DAY, "provider": "manual", "gift": False,
            "code": "", "referral": "", "tariff": {"name": "Month", "pack": False}}
    base.update(over)
    return base


class Money(unittest.TestCase):
    def test_every_day_is_there_even_an_empty_one(self):
        series = analytics.revenue_by_day([order("a@x.com", 100, 0), order("b@x.com", 50, 0),
                                           order("c@x.com", 70, 40)], NOW)
        self.assertEqual(len(series), 30)
        self.assertEqual(series[-1][1], 150)
        self.assertEqual(sum(a for _, a in series), 150)

    def test_months_run_back_a_year(self):
        months = analytics.revenue_by_month([order("a@x.com", 100, 0), order("a@x.com", 200, 31)], NOW)
        self.assertEqual(len(months), 12)
        self.assertEqual(months[-1], ("2026-10", 100, 1))
        self.assertEqual(months[-2], ("2026-09", 200, 1))

    def test_kinds_of_sale(self):
        split = analytics.breakdown([
            order("a@x.com", 100, 0),
            order("b@x.com", 50, 0, tariff={"name": "+50", "pack": True}),
            order("c@x.com", 300, 0, gift=True),
            order("d@x.com", 90, 0, code="SPRING"),
        ])
        self.assertEqual(split["kinds"], {"subscription": 190, "pack": 50, "gift": 300, "promo": 90})

    def test_summary(self):
        s = analytics.summary([order("a@x.com", 100, 1), order("a@x.com", 200, 60)], NOW)
        self.assertEqual((s["total"], s["recent_total"], s["payers"], s["average"]), (300, 100, 1, 150))


class People(unittest.TestCase):
    def test_conversion_counts_free_arrivals_who_paid(self):
        codes = [{"word": "FREE", "used_by": ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]},
                 {"word": "PROMO", "discount": 10, "used_by": ["z@x.com"]}]
        c = analytics.conversion([order("a@x.com", 100, 1), order("z@x.com", 90, 1)], codes)
        self.assertEqual((c["arrived"], c["paid"], c["percent"]), (4, 1, 25))

    def test_a_renewal_is_a_purchase_by_somebody_already_a_client(self):
        clients = [{"email": "old@x.com", "createdAt": NOW - 100 * DAY, "expiryTime": NOW + 20 * DAY},
                   {"email": "new@x.com", "createdAt": NOW - 2 * DAY, "expiryTime": NOW + 28 * DAY},
                   {"email": "gone@x.com", "createdAt": NOW - 90 * DAY, "expiryTime": NOW - 3 * DAY}]
        r = analytics.retention([order("old@x.com", 100, 5), order("new@x.com", 100, 2)], clients, NOW)
        self.assertEqual((r["renewals"], r["ended"], r["percent"]), (1, 1, 50))


if __name__ == "__main__":
    unittest.main()
