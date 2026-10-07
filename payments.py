"""
Orders: what somebody was offered, what they paid for, and whether it reached
3x-ui.

This is the one piece of client-related state that 3x-ui cannot hold for us.
An invoice exists before the client does, it lives at a payment provider, and
the only thing that stops a paid one from being applied twice — or not at all
after a restart — is a record of where it stands. Everything about the client
itself still lives in 3x-ui; an order only says "this much was paid for this
tariff, and here is how far we got".

An order moves one way:

    pending ──▶ paid ──▶ applied
       │
       └──▶ expired / cancelled

`pending` is an invoice handed out and not yet paid. `paid` is money received
and not yet turned into days in 3x-ui — usually for a moment, longer if 3x-ui
is unreachable, in which case the bot keeps trying. `applied` is the end.

An order carries a copy of the tariff it was for, taken when the offer went
out. What somebody paid for is what was on the page when they paid, not what
the tariff says by the time the money arrives: the same rule as a code word's
tariff being read once, at registration.

`payments.json` sits beside the other state files and is backed up and kept
across updates the same way — see STATE_FILES in mailchilla.sh.
"""
import json
import logging
import os
import secrets
import threading
import time

import tariffs

logger = logging.getLogger(__name__)

PAYMENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payments.json")

PENDING, PAID, APPLIED, EXPIRED, CANCELLED = "pending", "paid", "applied", "expired", "cancelled"
OPEN = (PENDING, PAID)
STATUSES = (PENDING, PAID, APPLIED, EXPIRED, CANCELLED)

# How many closed orders are kept. Open ones are always kept, whatever their
# number: dropping one would lose money somebody is about to pay or has paid.
# The closed ones are history, and payments.json is rewritten whole on every
# change — the same reasoning as USED_BY_KEPT in tariffs.py.
CLOSED_KEPT = 2000

# An order number is read off a letter and typed into a bank transfer's
# comment, so it is drawn from the same unambiguous alphabet a generated code
# word is.
ORDER_ID_LENGTH = 8

_lock = threading.RLock()
_orders = []
# Set when payments.json exists but could not be read. Writing is then
# refused: an empty list written over the broken file would erase whatever
# paid orders it still holds. The panel and the bot keep running; only the
# payments stop, loudly.
_broken = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    taken = {o["id"] for o in _orders}
    while True:
        candidate = "".join(secrets.choice(tariffs.GENERATED_ALPHABET)
                            for _ in range(ORDER_ID_LENGTH))
        if candidate not in taken:
            return candidate


def _clean(raw: dict) -> dict:
    status = raw.get("status") if raw.get("status") in STATUSES else PENDING
    return {
        "id": str(raw["id"]),
        # Several invoices go out in one letter — one per tariff and way of
        # paying. They share an offer id, and the first one paid closes the
        # rest, so a second tap in the same letter cannot be charged.
        "offer_id": str(raw.get("offer_id") or ""),
        "email": str(raw.get("email") or "").strip().lower(),
        "tariff": {
            "id": str((raw.get("tariff") or {}).get("id") or ""),
            "name": str((raw.get("tariff") or {}).get("name") or ""),
            "limit_gb": int((raw.get("tariff") or {}).get("limit_gb") or 0),
            "expire_days": int((raw.get("tariff") or {}).get("expire_days") or 0),
            "inbound_ids": [int(i) for i in (raw.get("tariff") or {}).get("inbound_ids") or []],
        },
        "amount": int(raw.get("amount") or 0),
        # The price before the discount, and the word that gave it. Equal to
        # the amount, and empty, for an order bought at the full price.
        "full_price": int(raw.get("full_price") or raw.get("amount") or 0),
        "code": str(raw.get("code") or ""),
        "currency": str(raw.get("currency") or "RUB"),
        "provider": str(raw.get("provider") or ""),
        # The provider's own id for the invoice, and the link to pay it.
        "provider_ref": str(raw.get("provider_ref") or ""),
        "pay_url": str(raw.get("pay_url") or ""),
        "status": status,
        "created_at": int(raw.get("created_at") or _now_ms()),
        "expires_at": int(raw.get("expires_at") or 0),
        "paid_at": int(raw.get("paid_at") or 0),
        "applied_at": int(raw.get("applied_at") or 0),
        # Who said it was paid: the provider's id, or "admin" for a hand mark.
        "paid_by": str(raw.get("paid_by") or ""),
        # The end of the term this order sets, worked out once, on the first
        # attempt to apply it, and stored before 3x-ui is touched. A retry sets
        # the same absolute date again instead of adding the days a second time.
        "target_expiry": raw.get("target_expiry"),
        "attempts": int(raw.get("attempts") or 0),
        "next_try_at": int(raw.get("next_try_at") or 0),
        "error": str(raw.get("error") or ""),
    }


def _trim(orders: list) -> list:
    closed = [o for o in orders if o["status"] not in OPEN]
    if len(closed) <= CLOSED_KEPT:
        return orders
    drop = {o["id"] for o in sorted(closed, key=lambda o: o["created_at"])[:len(closed) - CLOSED_KEPT]}
    return [o for o in orders if o["id"] not in drop]


def _write():
    """Writes the json atomically, so an interrupted write leaves no broken file."""
    global _orders
    if _broken:
        raise RuntimeError(f"{PAYMENTS_PATH} could not be read; payments are stopped until it is fixed")
    _orders = _trim(_orders)
    tmp_path = PAYMENTS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"orders": _orders}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, PAYMENTS_PATH)


def load():
    """Reads payments.json. A missing file is no orders yet, not an error."""
    global _orders, _broken
    with _lock:
        _broken = False
        if not os.path.exists(PAYMENTS_PATH):
            _orders = []
            return
        try:
            with open(PAYMENTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            logger.error(f"Could not read {PAYMENTS_PATH}: {e}. Payments are stopped until it is fixed.")
            _orders = []
            _broken = True
            return
        orders = []
        for raw in data.get("orders") or []:
            try:
                orders.append(_clean(raw))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"Skipping an unreadable order in payments.json: {e}")
        _orders = orders
        logger.info(f"Orders read from payments.json: {len(orders)}.")


def create(email: str, tariff: dict, provider: str, offer_id: str = "",
           hours: int = 24, code: dict = None) -> dict:
    """
    A new pending order for one tariff, paid one way. Not yet an invoice.

    `code` is the word the offer came through, when it carries a discount. The
    price is worked out here, once, like everything else the order copies.
    """
    with _lock:
        now = _now_ms()
        order = _clean({
            "id": _new_id(),
            "offer_id": offer_id,
            "email": email,
            "tariff": tariff,
            "amount": tariffs.discounted_price(tariff["price"], code),
            "full_price": tariff["price"],
            "code": (code or {}).get("word", "") if (code or {}).get("discount") else "",
            "provider": provider,
            "created_at": now,
            "expires_at": now + max(int(hours), 1) * 3600 * 1000,
        })
        _orders.append(order)
        _write()
        return dict(order)


def new_offer_id() -> str:
    return secrets.token_hex(4)


def get(order_id: str):
    with _lock:
        for o in _orders:
            if o["id"] == order_id:
                return json.loads(json.dumps(o))
    return None


def all_orders() -> list:
    """Newest first."""
    with _lock:
        return [json.loads(json.dumps(o))
                for o in sorted(_orders, key=lambda o: o["created_at"], reverse=True)]


def with_status(*statuses) -> list:
    with _lock:
        return [json.loads(json.dumps(o)) for o in _orders if o["status"] in statuses]


def update(order_id: str, **fields) -> dict:
    """Sets fields on one order and writes the file."""
    with _lock:
        for i, o in enumerate(_orders):
            if o["id"] == order_id:
                merged = dict(o)
                merged.update(fields)
                cleaned = _clean(merged)
                _orders[i] = cleaned
                # By reference, not by index: the write may trim old orders
                # and shift the list under it.
                _write()
                return json.loads(json.dumps(cleaned))
    raise KeyError(order_id)


def mark_paid(order_id: str, by: str) -> bool:
    """
    Records the money as received. True only the first time.

    The rest of the offer is closed at once: a letter carries a link per
    tariff and way of paying, and having paid through one of them somebody
    should not be able to pay through another by mistake.
    """
    with _lock:
        order = get(order_id)
        # Expired and cancelled orders count too: a provider that does not
        # honour our expiry may still take the money, and money that arrived
        # is money that arrived. Paid and applied ones are already counted.
        if not order or order["status"] in (PAID, APPLIED):
            return False
        # next_try_at served the invoice checks until now; from here it is the
        # schedule for applying, which starts at once.
        update(order_id, status=PAID, paid_at=_now_ms(), paid_by=by, error="",
               next_try_at=0, attempts=0)
        if order["offer_id"]:
            for other in _orders:
                if (other["offer_id"] == order["offer_id"] and other["id"] != order_id
                        and other["status"] == PENDING):
                    other["status"] = CANCELLED
            _write()
        logger.info(f"Order {order_id} for {order['email']} paid ({by}): "
                    f"{order['amount']} {order['currency']}, tariff {order['tariff']['name']!r}.")
        return True


def expire_stale(now_ms: int = None) -> int:
    """Closes the pending orders past their time. Returns how many."""
    now_ms = now_ms or _now_ms()
    with _lock:
        count = 0
        for o in _orders:
            if o["status"] == PENDING and o["expires_at"] and o["expires_at"] <= now_ms:
                o["status"] = EXPIRED
                count += 1
        if count:
            _write()
        return count
