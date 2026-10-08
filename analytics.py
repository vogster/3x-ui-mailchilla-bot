"""
The figures behind the Analytics page.

Everything here is worked out on request from what is already kept: the
applied orders in payments.json, the client list from 3x-ui, and who came in
through which word in tariffs.json. Nothing is stored for it, so nothing can
drift from the truth — a number on the page is always the one the data says
now. The functions are pure: they take the data and a clock, which is what
lets them be tested without a panel or a 3x-ui.

Money is what was applied, not what was paid: an order paid and stuck is a
problem to fix on the Payments page, not revenue yet.
"""
from collections import Counter, OrderedDict
from datetime import datetime, timedelta

DAY_MS = 86400 * 1000


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m")


def revenue_by_day(orders: list, now_ms: int, days: int = 30) -> list:
    """[(date, amount)], oldest first, one entry for every day including empty ones."""
    today = datetime.fromtimestamp(now_ms / 1000).date()
    series = OrderedDict(((today - timedelta(days=i)).isoformat(), 0) for i in range(days - 1, -1, -1))
    for order in orders:
        key = _day(order["applied_at"])
        if key in series:
            series[key] += order["amount"]
    return list(series.items())


def revenue_by_month(orders: list, now_ms: int, months: int = 12) -> list:
    """[(YYYY-MM, amount, count)], oldest first, the last `months` calendar months."""
    current = datetime.fromtimestamp(now_ms / 1000).replace(day=1)
    keys = []
    year, month = current.year, current.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    keys.reverse()
    amounts, counts = Counter(), Counter()
    for order in orders:
        key = _month(order["applied_at"])
        amounts[key] += order["amount"]
        counts[key] += 1
    return [(k, amounts[k], counts[k]) for k in keys]


def breakdown(orders: list) -> dict:
    """Revenue split by tariff and by way of paying, and the share of each kind of sale."""
    by_tariff, by_provider = Counter(), Counter()
    kinds = Counter()
    for order in orders:
        by_tariff[order["tariff"]["name"]] += order["amount"]
        by_provider[order["provider"]] += order["amount"]
        if order["gift"]:
            kinds["gift"] += order["amount"]
        elif order["tariff"].get("pack"):
            kinds["pack"] += order["amount"]
        else:
            kinds["subscription"] += order["amount"]
        if order.get("code"):
            kinds["promo"] += order["amount"]
        if order.get("referral"):
            kinds["referral"] += order["amount"]
    return {
        "by_tariff": by_tariff.most_common(),
        "by_provider": by_provider.most_common(),
        "kinds": dict(kinds),
    }


def summary(orders: list, now_ms: int, days: int = 30) -> dict:
    """Totals for the whole record and for the last `days`."""
    since = now_ms - days * DAY_MS
    recent = [o for o in orders if o["applied_at"] >= since]
    payers = {o["email"] for o in orders}
    total = sum(o["amount"] for o in orders)
    return {
        "total": total,
        "orders": len(orders),
        "payers": len(payers),
        "average": round(total / len(orders)) if orders else 0,
        "recent_total": sum(o["amount"] for o in recent),
        "recent_orders": len(recent),
        "recent_payers": len({o["email"] for o in recent}),
    }


def conversion(orders: list, codes: list) -> dict:
    """
    Of everybody who came in through a free word, how many have paid since.

    A free word is one with neither a discount nor bonus days attached, nor
    made by a gift: the people who arrived on a trial. The answer is as good as
    the codes' used_by record, which keeps the last USED_BY_KEPT addresses of a
    word — on a word used by more, the oldest arrivals are not counted either way.
    """
    payers = {o["email"] for o in orders if not o["gift"]}
    arrived = set()
    for code in codes:
        if code.get("discount") or code.get("bonus_days") or code.get("gift_order"):
            continue
        arrived.update(str(a).strip().lower() for a in code.get("used_by") or [])
    converted = arrived & payers
    return {
        "arrived": len(arrived),
        "paid": len(converted),
        "percent": round(100 * len(converted) / len(arrived)) if arrived else None,
    }


def retention(orders: list, clients: list, now_ms: int, days: int = 30) -> dict:
    """
    Over the last `days`: renewals, and subscriptions that ended and stayed ended.

    A renewal is an applied purchase by somebody who was a client before they
    ordered — not a first purchase, not a gift, not a pack. An ended one is a
    client whose end fell in the window and is still in the past: whoever
    renewed has an end in the future again and does not count here.
    """
    since = now_ms - days * DAY_MS
    created = {}
    for client in clients:
        # The 3x-ui identifier, read back as the address orders are kept under.
        from xui_client import XuiClient
        address = XuiClient.extract_bare_email(str(client.get("email") or "")).lower()
        if address:
            created[address] = int(client.get("createdAt") or 0)
    renewals = 0
    for order in orders:
        if order["applied_at"] < since or order["gift"] or order["tariff"].get("pack"):
            continue
        made = created.get(order["email"], 0)
        if made and made < order["created_at"] - DAY_MS:
            renewals += 1
    ended = sum(1 for c in clients
                if since <= int(c.get("expiryTime") or 0) <= now_ms)
    return {"renewals": renewals, "ended": ended,
            "percent": (round(100 * renewals / (renewals + ended)) if renewals + ended else None)}
