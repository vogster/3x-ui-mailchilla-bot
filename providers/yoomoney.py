"""
YooMoney — a card payment into a YooMoney wallet, through its Quickpay form.

Open to anybody with a wallet, whatever their status, and that is its whole
case: there is no contract, and so no receipts either — a seller who owes
receipts issues them themselves.

The link is built here, with no request: Quickpay is a form, and its
parameters ride in the address. That is also its weakness. The address can be
edited before paying — the sum lowered, our order number kept in the label —
so a payment is never counted for its label alone: the amount that arrived
has to be the price, less what YooMoney keeps.

Whether it arrived is asked of the wallet's operation history, filtered by the
label. That needs an OAuth token with the `operation-history` scope, issued
for the wallet's owner.
"""
import logging
from urllib.parse import urlencode

import requests

import config
import payments
from providers.base import Invoice, Provider

logger = logging.getLogger(__name__)

FORM = "https://yoomoney.ru/quickpay/confirm.xml"
HISTORY = "https://yoomoney.ru/api/operation-history"
TIMEOUT = 15

# YooMoney takes its commission from the recipient: 3% of a card payment.
# What arrives is the price less that, and anything below it is a link
# somebody edited. The extra kopek is for rounding.
COMMISSION = 0.03


class YooMoney(Provider):
    id = "yoomoney"
    title = "ЮMoney"
    # A Quickpay link has no lifetime of its own.
    link_expires = False

    def enabled(self) -> bool:
        return bool(getattr(config, "YOOMONEY_ENABLED", False)
                    and (getattr(config, "YOOMONEY_WALLET", "") or "").strip()
                    and (getattr(config, "YOOMONEY_TOKEN", "") or "").strip())

    def create(self, order: dict) -> Invoice:
        query = urlencode({
            "receiver": config.YOOMONEY_WALLET.strip(),
            "quickpay-form": "button",
            "paymentType": "AC",
            "sum": order["amount"],
            "label": order["id"],
            "targets": f"{config.SERVICE_NAME}: {order['tariff']['name']}",
        })
        return Invoice(url=f"{FORM}?{query}", ref=order["id"])

    def _history(self, **params) -> dict:
        response = requests.post(HISTORY, timeout=TIMEOUT, data=params,
                                 headers={"Authorization": f"Bearer {config.YOOMONEY_TOKEN.strip()}"})
        if response.status_code == 401:
            raise RuntimeError("the token was refused — issue a new one with the operation-history scope")
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"operation-history: {body['error']}")
        return body

    def probe(self) -> str:
        # The token is the part that can be wrong in a way nothing else shows:
        # the link is built without it, and only the check needs it.
        self._history(type="deposition", records=1)
        return ""

    def check(self, order: dict):
        body = self._history(type="deposition", label=order["id"], records=10)
        least = order["amount"] * (1 - COMMISSION) - 0.01
        for operation in body.get("operations") or []:
            if operation.get("label") != order["id"] or operation.get("status") != "success":
                continue
            if float(operation.get("amount") or 0) >= least:
                return payments.PAID
            logger.warning(f"Order {order['id']}: {operation.get('amount')} ₽ arrived for a price of "
                           f"{order['amount']} ₽ — less than the price less the commission, so it is "
                           f"not counted. Check it in the wallet and mark the order paid by hand if it is right.")
        return payments.PENDING
