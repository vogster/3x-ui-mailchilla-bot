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

# Waits between attempts to apply a paid order that 3x-ui refused, doubling up
# to the last. Each attempt is a line in the log, and an unreachable 3x-ui
# should not write one every few seconds.
RETRY_MS = [60_000, 120_000, 300_000, 900_000, 1_800_000]


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- The offer -------------------------------------------------------------

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
    hours = int(getattr(config, "PAYMENT_INVOICE_HOURS", 24) or 24)
    blocks = []
    for tariff in for_sale:
        entries = []
        for provider in ways:
            order = payments.create(email, tariff, provider.id, offer_id=offer_id,
                                    hours=hours, code=code)
            try:
                invoice = provider.create(order)
            except Exception as e:
                logger.error(f"{provider.title} could not create an invoice for {email} "
                             f"({tariff['name']!r}): {e}")
                payments.update(order["id"], status=payments.CANCELLED, error=str(e)[:300])
                continue
            payments.update(order["id"], provider_ref=invoice.ref, pay_url=invoice.url)
            entries.append({
                "order": order["id"],
                "provider": provider.title,
                "url": invoice.url,
            })
        if entries:
            blocks.append({"tariff": tariff, "ways": entries,
                           "price": tariffs.discounted_price(tariff["price"], code)})
    return blocks or None


def send_offer(email: str, code: dict = None):
    """
    The answer to /buy, or to a word carrying a discount: the tariffs for sale
    with a way to pay each.
    """
    blocks = build_offer(email, code)
    if not blocks:
        logger.info(f"{email} asked to buy, but there is nothing on sale "
                    f"(tariffs with a price: {len(tariffs.for_sale())}, "
                    f"ways of paying switched on: {len(providers.enabled())}).")
        mailer.send_email_reply(email, templates.notice_subject("not_for_sale"),
                                templates.get_notice("not_for_sale"))
        return
    mailer.send_email_reply(email, templates.text("offer.subject"),
                            templates.get_offer_email(blocks, code))
    logger.info(f"Offer sent to {email}: {len(blocks)} tariff(s)"
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
    for order in payments.with_status(payments.PENDING):
        provider = providers.get(order["provider"])
        if not provider or not provider.polls:
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
        elif status == payments.EXPIRED:
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
    """
    days = order["tariff"]["expire_days"]
    if days <= 0:
        return 0
    current = int((client or {}).get("expiryTime") or 0)
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
    if payments.with_status(*payments.OPEN):
        poll()
