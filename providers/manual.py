"""
Paying by hand: a transfer to the details in the letter, confirmed by the
administrator on the Payments page.

Costs nothing, needs no contract and no status, and is what most small
services start with. The order number goes into the letter so that a transfer
can be matched to it; nothing here can see the money arrive.
"""
import config
import i18n
from providers.base import Invoice, Provider


class Manual(Provider):
    id = "manual"
    polls = False

    @property
    def title(self):
        return i18n.t("By transfer")

    def enabled(self) -> bool:
        return bool(getattr(config, "PAYMENT_MANUAL_ENABLED", False)
                    and (getattr(config, "PAYMENT_MANUAL_DETAILS", "") or "").strip())

    def create(self, order: dict) -> Invoice:
        return Invoice(url="", ref="")

    def check(self, order: dict):
        return None
