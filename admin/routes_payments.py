"""
The Payments page: every order, and the three things a person may do to one.

Marking paid is how paying by transfer works at all — nothing else can see a
transfer arrive — and it is also the way out when a provider took the money
but its status never came through. Applying again is for a paid order 3x-ui
kept refusing. Cancelling closes an invoice nobody should pay any more.
"""
import logging
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import i18n
import payments
import providers
import purchases
import tariffs
from admin.deps import templates, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()

STATUS_LABELS = {
    payments.PENDING: "awaiting payment",
    payments.PAID: "paid, not applied yet",
    payments.APPLIED: "applied [order]",
    payments.EXPIRED: "expired [order]",
    payments.CANCELLED: "cancelled [order]",
}


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y %H:%M") if ms else ""


def _row(order: dict) -> dict:
    provider = providers.get(order["provider"])
    return {
        **order,
        "provider_title": provider.title if provider else order["provider"],
        "status_text": i18n.t(STATUS_LABELS[order["status"]]),
        "created_text": _fmt(order["created_at"]),
        "paid_text": _fmt(order["paid_at"]),
        "until_text": (_fmt(order["target_expiry"])[:10] if order["target_expiry"]
                       else ""),
    }


@router.get("/payments", response_class=HTMLResponse)
def payments_list(request: Request, status: str = "", saved: str = "", error: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    orders = payments.all_orders()
    counts = {s: sum(1 for o in orders if o["status"] == s) for s in payments.STATUSES}
    if status in payments.STATUSES:
        orders = [o for o in orders if o["status"] == status]
    else:
        status = ""
    # The money that actually turned into days, which is the figure that
    # means something: paid-but-stuck is a problem to fix, not revenue yet.
    applied = payments.with_status(payments.APPLIED)
    return templates.TemplateResponse("payments.html", {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "orders": [_row(o) for o in orders],
        "counts": counts,
        "status": status,
        "status_labels": {k: i18n.t(v) for k, v in STATUS_LABELS.items()},
        "revenue": sum(o["amount"] for o in applied),
        "for_sale": len(tariffs.for_sale()),
        "ways": [p.title for p in providers.enabled()],
        "saved": saved,
        "error": error,
    })


def _back(message: str = "", error: str = "") -> RedirectResponse:
    query = f"?saved={quote(message)}" if message else (f"?error={quote(error)}" if error else "")
    return RedirectResponse(f"/payments{query}", status_code=303)


@router.post("/payments/{order_id}/{action}")
def payment_action(request: Request, order_id: str, action: str):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    order = payments.get(order_id)
    if not order:
        return _back(error=i18n.t("No order {order}", order=order_id))
    try:
        if action == "paid":
            if not payments.mark_paid(order_id, "admin"):
                return _back(error=i18n.t("Order {order} is already paid", order=order_id))
            logger.info(f"Order {order_id} marked paid by hand in the panel.")
            if purchases.apply(order_id):
                return _back(i18n.t("Order {order} is paid and applied", order=order_id))
            return _back(error=i18n.t("Order {order} is marked paid, but 3x-ui refused the change; "
                                      "the bot keeps trying", order=order_id))
        if action == "apply":
            if purchases.apply(order_id):
                return _back(i18n.t("Order {order} is paid and applied", order=order_id))
            return _back(error=i18n.t("3x-ui refused the change again; see the log"))
        if action == "cancel":
            if order["status"] != payments.PENDING:
                return _back(error=i18n.t("Only an order awaiting payment can be cancelled"))
            payments.update(order_id, status=payments.CANCELLED)
            logger.info(f"Order {order_id} cancelled in the panel.")
            return _back(i18n.t("Order {order} is cancelled", order=order_id))
    except (OSError, RuntimeError) as e:
        logger.error(f"Could not change order {order_id}: {e}")
        return _back(error=str(e))
    return _back()
