"""
Crypto Pay — the payment API of @CryptoBot in Telegram.

The invoice is priced in rubles and paid in whatever coin the client has; the
conversion is Crypto Pay's business. Needs nothing but a token from the bot
(Crypto Pay → My Apps → Create App), which is why it is the easiest online
way of paying to set up: no contract, no status, no checks.

The amount is fixed on Crypto Pay's side when the invoice is created, so a
paid invoice is a paid order — there is nothing in the link a client could
edit to pay less.
"""
import logging

import requests

import config
import payments
from providers.base import Invoice, Provider

logger = logging.getLogger(__name__)

MAINNET = "https://pay.crypt.bot/api"
TESTNET = "https://testnet-pay.crypt.bot/api"
TIMEOUT = 15


class CryptoPay(Provider):
    id = "cryptopay"
    title = "Crypto Pay"

    def enabled(self) -> bool:
        return bool(getattr(config, "CRYPTOPAY_ENABLED", False)
                    and (getattr(config, "CRYPTOPAY_TOKEN", "") or "").strip())

    def _call(self, method: str, params: dict):
        base = TESTNET if getattr(config, "CRYPTOPAY_TESTNET", False) else MAINNET
        response = requests.post(f"{base}/{method}", json=params, timeout=TIMEOUT,
                                 headers={"Crypto-Pay-API-Token": config.CRYPTOPAY_TOKEN})
        body = response.json()
        if not body.get("ok"):
            error = body.get("error") or {}
            raise RuntimeError(f"{method}: {error.get('name') or error or response.status_code}")
        return body["result"]

    def probe(self) -> str:
        me = self._call("getMe", {})
        return str(me.get("name") or me.get("app_id") or "")

    def create(self, order: dict) -> Invoice:
        hours = max(1, (order["expires_at"] - order["created_at"]) // 3_600_000)
        result = self._call("createInvoice", {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(order["amount"]),
            "description": f"{config.SERVICE_NAME}: {order['tariff']['name']}"[:1024],
            # Our order number, so an invoice found in the Crypto Pay app can
            # be traced back without asking us.
            "payload": order["id"],
            # The same lifetime as the order: a link that outlives it would
            # take money for an order already closed.
            "expires_in": int(hours * 3600),
            "allow_comments": False,
        })
        url = (result.get("bot_invoice_url") or result.get("mini_app_invoice_url")
               or result.get("web_app_invoice_url") or result.get("pay_url") or "")
        if not url:
            raise RuntimeError("createInvoice returned no link")
        return Invoice(url=url, ref=str(result["invoice_id"]))

    def check(self, order: dict):
        if not order["provider_ref"]:
            return None
        result = self._call("getInvoices", {"invoice_ids": order["provider_ref"]})
        # The documentation says an array; the API answers {"items": [...]}.
        items = result.get("items", []) if isinstance(result, dict) else result
        for item in items or []:
            if str(item.get("invoice_id")) != order["provider_ref"]:
                continue
            status = item.get("status")
            if status == "paid":
                return payments.PAID
            if status == "expired":
                return payments.EXPIRED
            return payments.PENDING
        return None
