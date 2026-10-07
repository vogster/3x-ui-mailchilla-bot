"""
Selling a subscription: the offer letter, watching the invoices, and turning a
paid one into days in 3x-ui.

Three steps, kept apart because each fails differently:

- `send_offer` creates an invoice per tariff for sale and way of paying, and
  mails the links. A provider that refuses is left out of the letter rather
  than stopping it.
- `poll` asks the providers how their open invoices stand. It runs from the
  bot loop, because the panel cannot be reached from outside (see
  providers/base.py) — the same reason the mailbox is polled rather than
  pushed to.
- `apply` is the step that must happen exactly once per payment, and is
  written so that it can be retried as often as it takes: the date it sets is
  worked out on the first attempt and stored, so a second attempt sets the
  same date instead of adding the days again.

What a purchase does to a client: the term runs from whichever is later, today
or the end of what they already have, so paying early loses nothing; the
traffic counter starts from zero; the client's group becomes the tariff
bought. Somebody who is not a client yet becomes one.
"""
import logging
import time
from datetime import datetime

import config
import i18n
import mailer
import payments
import providers
import tariffs
import templates
from xui_client import get_shared_client

logger = logging.getLogger(__name__)

DAY_MS = 86400 * 1000

# How often one open invoice is asked about. The bot polls the mailbox every
# few seconds; a provider asked that often about every invoice in every letter
# would rate-limit us, and nobody needs their payment noticed within seconds.
CHECK_EVERY_MS = 60 * 1000

# How long an expired order is still watched, for a provider whose link keeps
# working after ours has run out. Somebody who opened the letter late and paid
# anyway has paid; three days covers a weekend away from the mail.
LATE_PAYMENT_MS = 3 * 86400 * 1000

# Waits between attempts to apply a paid order that 3x-ui refused, doubling up
# to the last. Each attempt is a line in the log, and an unreachable 3x-ui
# should not write one every few seconds.
RETRY_MS = [60_000, 120_000, 300_000, 900_000, 1_800_000]


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- The offer -------------------------------------------------------------

# An offer still open is sent again rather than replaced, as long as this much
# of it is left. Less than that, and the reader could open the letter to links
# that have just died; a fresh offer is the better answer.
REUSE_LEFT_MS = 2 * 3600 * 1000


def is_barred(client) -> bool:
    """
    Whether this client was switched off by hand, and so may not buy.

    3x-ui switches a client off by itself when its term or its traffic runs
    out — exactly the people an offer is for. So a disabled client is only
    barred when neither has happened: then somebody switched them off on
    purpose, and a purchase would quietly undo that decision.
    """
    if not client or client.get("enable") is not False:
        return False
    expiry = int(client.get("expiryTime") or 0)
    if expiry > 0 and expiry <= _now_ms():
        return False
    total = int(client.get("totalGB") or 0)
    traffic = client.get("traffic") or {}
    used = int(traffic.get("up") or client.get("up") or 0) + int(traffic.get("down") or client.get("down") or 0)
    if total > 0 and used >= total:
        return False
    return True


def _lifetime_hours(provider) -> int:
    """How long an invoice through this provider lives: ours, unless it allows less."""
    hours = int(getattr(config, "PAYMENT_INVOICE_HOURS", 24) or 24)
    return min(hours, provider.max_hours) if provider.max_hours else hours


def _open_offer(email: str, code: dict = None):
    """
    The orders of an offer this address already holds, if one is still good.

    Answering every /buy with fresh invoices made each repeated letter a new
    row at every provider, and two letters' worth of links that could both be
    paid. The same links again are what the reader actually needs. An offer
    made through a different word — or through none — is a different offer.
    """
    word = (code or {}).get("word", "") if (code or {}).get("discount") else ""
    now = _now_ms()
    by_offer = {}
    for order in payments.with_status(payments.PENDING):
        if order["email"] == email.strip().lower() and order["offer_id"]:
            by_offer.setdefault(order["offer_id"], []).append(order)
    for orders in by_offer.values():
        if orders[0]["code"] != word:
            continue
        if min(o["expires_at"] for o in orders) - now < REUSE_LEFT_MS:
            continue
        if any(not providers.get(o["provider"]) or not providers.get(o["provider"]).enabled()
               for o in orders):
            # A way of paying switched off since: its link is not to be sent again.
            continue
        return sorted(orders, key=lambda o: o["created_at"])
    return None


def _blocks(orders: list) -> list:
    """Orders laid out the way the letter wants them: one block per tariff."""
    blocks = {}
    for order in orders:
        block = blocks.setdefault(order["tariff"]["id"], {
            "tariff": {**order["tariff"], "price": order["full_price"]},
            "price": order["amount"],
            "ways": [],
        })
        provider = providers.get(order["provider"])
        block["ways"].append({
            "order": order["id"],
            "provider": provider.title if provider else order["provider"],
            "url": order["pay_url"],
            "expires_at": order["expires_at"],
        })
    return list(blocks.values())


def build_offer(email: str, code: dict = None):
    """
    Creates the invoices for one offer letter.

    Returns a list of {"tariff": …, "ways": [...]}, one per tariff for sale,
    or None when there is nothing to offer — no tariff has a price, or no way
    of paying is switched on.

    With a code — a word carrying a discount — the letter offers that word's
    tariff alone, at the lower price. Listing the others at full price beside
    it would bury the one thing the reader wrote in for.
    """
    if code:
        tariff = tariffs.get(code["tariff_id"])
        for_sale = [tariff] if tariff and tariff["price"] else []
    else:
        for_sale = tariffs.for_sale()
    ways = providers.enabled()
    if not for_sale or not ways:
        return None

    offer_id = payments.new_offer_id()
    made = []
    for tariff in for_sale:
        for provider in ways:
            order = payments.create(email, tariff, provider.id, offer_id=offer_id,
                                    hours=_lifetime_hours(provider), code=code)
            try:
                invoice = provider.create(order)
            except Exception as e:
                logger.error(f"{provider.title} could not create an invoice for {email} "
                             f"({tariff['name']!r}): {e}")
                payments.update(order["id"], status=payments.CANCELLED, error=str(e)[:300])
                continue
            made.append(payments.update(order["id"], provider_ref=invoice.ref, pay_url=invoice.url))
    return _blocks(made) or None


def send_offer(email: str, code: dict = None):
    """
    The answer to /buy, or to a word carrying a discount: the tariffs for sale
    with a way to pay each.
    """
    client = get_shared_client().find_client_by_email(email)
    if is_barred(client):
        logger.info(f"{email} asked to buy, but was switched off by hand in 3x-ui; refused.")
        mailer.send_email_reply(email, templates.notice_subject("purchase_barred"),
                                templates.get_notice("purchase_barred"))
        return

    reused = _open_offer(email, code)
    blocks = _blocks(reused) if reused else build_offer(email, code)
    if not blocks:
        logger.info(f"{email} asked to buy, but there is nothing on sale "
                    f"(tariffs with a price: {len(tariffs.for_sale())}, "
                    f"ways of paying switched on: {len(providers.enabled())}).")
        mailer.send_email_reply(email, templates.notice_subject("not_for_sale"),
                                templates.get_notice("not_for_sale"))
        return
    mailer.send_email_reply(email, templates.text("offer.subject"),
                            templates.get_offer_email(blocks, code, now_ms=_now_ms()))
    logger.info(f"Offer {'sent again' if reused else 'sent'} to {email}: {len(blocks)} tariff(s)"
                + (f", word {code['word']!r} at {tariffs.discount_text(code)} off." if code else "."))


# --- Watching the invoices -------------------------------------------------

def poll():
    """
    One pass: close what has expired, ask about what is open, apply what is paid.

    Cheap when there is nothing open, which is nearly always — no request goes
    anywhere unless an invoice is waiting.
    """
    payments.expire_stale()
    now = _now_ms()
    for order in payments.with_status(payments.PENDING, payments.EXPIRED):
        provider = providers.get(order["provider"])
        if not provider or not provider.polls:
            continue
        if order["status"] == payments.EXPIRED and (
                provider.link_expires or now - order["expires_at"] > LATE_PAYMENT_MS):
            continue
        if order["next_try_at"] > now:
            continue
        payments.update(order["id"], next_try_at=now + CHECK_EVERY_MS)
        try:
            status = provider.check(order)
        except Exception as e:
            logger.warning(f"{provider.title} could not say how order {order['id']} stands: {e}")
            continue
        if status == payments.PAID:
            payments.mark_paid(order["id"], provider.id)
        elif status == payments.EXPIRED and order["status"] == payments.PENDING:
            payments.update(order["id"], status=payments.EXPIRED)
    apply_due()


def apply_due():
    """Applies every paid order whose turn has come."""
    now = _now_ms()
    for order in payments.with_status(payments.PAID):
        if order["next_try_at"] <= now:
            apply(order["id"])


# --- Applying a payment ----------------------------------------------------

def _target_expiry(order: dict, client) -> int:
    """
    The end of the term this payment buys, 0 for no end.

    From whichever is later: today, or the end of what the client has. Paying
    a week early must not cost a week. An already expired subscription counts
    from today, not from the day it ran out.

    A client whose subscription never ends keeps it that way: a payment buys
    time, and taking "for ever" away in exchange for thirty days is not what
    anybody paid for. A negative expiry is 3x-ui's "this long from the first
    connection", a term not yet started, and it is added to rather than lost.
    """
    days = order["tariff"]["expire_days"]
    if days <= 0:
        return 0
    if client is not None:
        current = int(client.get("expiryTime") or 0)
        if current == 0:
            return 0
        if current < 0:
            return _now_ms() - current + days * DAY_MS
    else:
        current = 0
    return max(_now_ms(), current) + days * DAY_MS


def apply(order_id: str) -> bool:
    """
    Turns a paid order into the client's new term in 3x-ui. True when done.

    Safe to call again after any failure, and after a restart halfway through:
    the date is stored on the order before 3x-ui is touched, so whichever
    attempt finally gets through sets that same date.
    """
    # Here rather than at the top: email_bot imports this module for /buy, and
    # the address helpers live there.
    from email_bot import build_client_email

    order = payments.get(order_id)
    if not order or order["status"] != payments.PAID:
        return False
    tariff = order["tariff"]
    email = order["email"]

    xui = get_shared_client()
    client = xui.find_client_by_email(email)

    target = order["target_expiry"]
    if target is None:
        target = _target_expiry(order, client)
        order = payments.update(order_id, target_expiry=target)

    if client:
        ok = xui.update_client(xui.client_key(client), total_gb=tariff["limit_gb"],
                               expiry_ms=target, enable=True, group=tariff["name"],
                               client_obj=client)
        ok = ok and xui.reset_traffic(client.get("email", ""))
        # The tariff's inbounds too: a client moved from one tariff to another
        # keeps the group's name only if they also get what that name stands
        # for. Done after the update, from the client as read before it — the
        # update changes nothing set_client_inbounds looks at.
        ok = ok and xui.set_client_inbounds(xui.client_key(client), tariff["inbound_ids"],
                                            client_obj=client)
        created = False
    else:
        new_uuid, _ = xui.add_client(email=build_client_email(email),
                                     limit_gb=tariff["limit_gb"], expiry_ms=target,
                                     inbound_ids=tariff["inbound_ids"], group=tariff["name"])
        ok = bool(new_uuid)
        created = True

    if not ok:
        attempts = order["attempts"] + 1
        wait = RETRY_MS[min(attempts, len(RETRY_MS)) - 1]
        payments.update(order_id, attempts=attempts, next_try_at=_now_ms() + wait,
                        error=i18n.t("3x-ui refused the change; trying again"))
        logger.error(f"Order {order_id} for {email} is paid but could not be applied in 3x-ui "
                     f"(attempt {attempts}); trying again in {wait // 60000} min.")
        return False

    payments.update(order_id, status=payments.APPLIED, applied_at=_now_ms(), error="")
    # The word is spent only now, once the purchase has happened — the same
    # rule as a registration: a code burned on a payment that never reached
    # 3x-ui would be worse than one used twice.
    if order["code"]:
        tariffs.spend(order["code"], email)
    logger.info(f"Order {order_id} applied: {email} is on {tariff['name']!r} until "
                f"{_fmt_date(target) or 'no end'}.")
    _tell(order, target, created)
    return True


def _fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y") if ms else ""


def _tell(order: dict, target: int, created: bool):
    """The receipt letter and the administrator's push. Neither undoes the payment if it fails."""
    email = order["email"]
    sub_url = ""
    if created:
        client = get_shared_client().find_client_by_email(email)
        if client:
            sub_url = f"{config.XUI_SUBSCRIPTION_BASE_URL}/{client.get('subId')}"
    try:
        mailer.send_email_reply(email, templates.text("paid.subject"),
                                templates.get_paid_email(order["id"], order["tariff"]["name"],
                                                         _fmt_date(target), sub_url))
    except Exception as e:
        logger.error(f"Order {order['id']} is applied, but the letter to {email} did not go: {e}")
    mailer.send_gotify_notification(
        title=i18n.t("Payment received"),
        message=i18n.t("{email} paid {amount} ₽ for {tariff}.", email=email,
                       amount=order["amount"], tariff=order["tariff"]["name"]),
    )


def run_if_due():
    """The bot loop's entry point. Nothing to do and nothing asked when nothing is open."""
    now = _now_ms()
    late = [o for o in payments.with_status(payments.EXPIRED)
            if now - o["expires_at"] <= LATE_PAYMENT_MS]
    if payments.with_status(*payments.OPEN) or late:
        poll()
