"""
What every way of paying has to be able to do.

Two things, and both from our side: create an invoice and ask how it stands.
There is no third, "be told", on purpose. The panel listens on 127.0.0.1, so a
provider's webhook cannot reach it without a domain, a certificate and a proxy
that most installations do not have — asking is what works everywhere. A
webhook can be added later as a shortcut to the same `mark_paid`, never as the
only way an order gets paid.
"""
from collections import namedtuple

# What the letter needs from a fresh invoice: where to send the reader, and
# the provider's own id to ask about it later. A provider with nothing to
# follow — paying by hand — returns an empty url and the letter shows the
# details instead.
Invoice = namedtuple("Invoice", ["url", "ref"])


class Provider:
    # A short fixed name, stored on every order. Never rename one: orders
    # already written under it would stop finding their provider.
    id = ""
    # The name as people know it: a brand, so not translated.
    title = ""
    # Whether check() can say anything. A provider that cannot is left out of
    # the poll, and its orders are marked paid in the panel.
    polls = True

    def enabled(self) -> bool:
        """Switched on and configured well enough to take money."""
        raise NotImplementedError

    def create(self, order: dict) -> Invoice:
        """An invoice for the order. Raises on failure; the letter then leaves this way out."""
        raise NotImplementedError

    def check(self, order: dict):
        """
        How the invoice stands at the provider: payments.PAID, payments.PENDING
        or payments.EXPIRED — or None for "could not ask", which changes nothing.
        """
        raise NotImplementedError
