"""
The ways a client can pay, one module each.

Adding one is a module with a Provider subclass, its settings, and a line in
ALL below. Its `id` is stored on every order it creates, so it is fixed for
good once released.
"""
from providers.cryptopay import CryptoPay
from providers.heleket import Heleket
from providers.manual import Manual
from providers.yoomoney import YooMoney

# The order the letter lists them in: the card first, since it is what most
# people reach for; then the crypto ones; paying by hand last.
ALL = [YooMoney(), CryptoPay(), Heleket(), Manual()]
_BY_ID = {p.id: p for p in ALL}


def get(provider_id: str):
    return _BY_ID.get(provider_id)


def enabled() -> list:
    """The providers that can take money right now, in the order the letter lists them."""
    return [p for p in ALL if p.enabled()]
