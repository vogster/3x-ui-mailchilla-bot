"""
Heleket — a crypto payment gateway with a payment page of its own.

Every request is signed: the header `sign` is md5 of the base64 of the exact
request body followed by the API key. "Exact" is the point — the body is
serialised once, signed, and those same bytes are sent; letting the HTTP
library serialise it again could space or escape it differently and the
signature would no longer match.

The amount is fixed on Heleket's side, so a client cannot pay less through
an edited link. Paying less in the blockchain is possible, and Heleket calls
it `wrong_amount`, which is not a final status: the order stays open until
the rest arrives or the invoice runs out.
"""
import base64
import hashlib
import json
import logging

import requests

import config
import payments
from providers.base import Invoice, Provider

logger = logging.getLogger(__name__)

BASE = "https://api.heleket.com/v1"
TIMEOUT = 15

PAID = {"paid", "paid_over"}
# Final and unpaid: the client did not pay, or something broke. A refund is
# a decision the administrator made in Heleket, and is left to them.
DEAD = {"cancel", "fail", "system_fail"}


def sign(body: bytes, api_key: str) -> str:
    return hashlib.md5(base64.b64encode(body) + api_key.encode()).hexdigest()


class Heleket(Provider):
    id = "heleket"
    title = "Heleket"
    # Heleket keeps an invoice for 5 minutes to 12 hours.
    max_hours = 12

    def enabled(self) -> bool:
        return bool(getattr(config, "HELEKET_ENABLED", False)
                    and (getattr(config, "HELEKET_MERCHANT", "") or "").strip()
                    and (getattr(config, "HELEKET_API_KEY", "") or "").strip())

    def _call(self, path: str, params: dict):
        body = json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode()
        response = requests.post(f"{BASE}/{path}", data=body, timeout=TIMEOUT, headers={
            "merchant": config.HELEKET_MERCHANT,
            "sign": sign(body, config.HELEKET_API_KEY),
            "Content-Type": "application/json",
        })
        reply = response.json()
        if reply.get("state") != 0 or "result" not in reply:
            raise RuntimeError(f"{path}: {reply.get('message') or reply.get('errors') or response.status_code}")
        return reply["result"]

    def probe(self) -> str:
        # The list of networks the merchant accepts: signed like everything
        # else, so a wrong key fails here the way it would on an invoice.
        services = self._call("payment/services", {})
        return str(len(services or []))

    def create(self, order: dict) -> Invoice:
        lifetime = (order["expires_at"] - order["created_at"]) // 1000
        result = self._call("payment", {
            "amount": str(order["amount"]),
            "currency": "RUB",
            "order_id": order["id"],
            # The order already lives no longer than max_hours; the clamp is
            # for the lower bound.
            "lifetime": int(min(max(lifetime, 300), 43200)),
            # Paying in instalments would leave an order half paid; one
            # payment of the whole amount is what the tariff costs.
            "is_payment_multiple": False,
        })
        if not result.get("url"):
            raise RuntimeError("payment returned no link")
        return Invoice(url=result["url"], ref=str(result.get("uuid") or ""))

    def check(self, order: dict):
        if not order["provider_ref"]:
            return None
        result = self._call("payment/info", {"uuid": order["provider_ref"]})
        status = result.get("payment_status") or result.get("status")
        if status in PAID:
            return payments.PAID
        if status in DEAD:
            return payments.EXPIRED
        return payments.PENDING
