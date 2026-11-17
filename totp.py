"""
The second step of signing in: a six-digit code from an authenticator app.

TOTP, RFC 6238 — the scheme every authenticator app speaks: a secret shared
once, as a QR code, and from then on a code worked out of that secret and the
time, new every thirty seconds. Written here in a screenful rather than taken
from a package, because a screenful is all it is: an HMAC, eight bytes of
counter and a truncation. The panel is the one place that can read a mailbox
password or delete every client, and what stood between it and anybody who
learned one password was that password.

Nothing in this module knows about settings or the panel. It is handed a
secret and a code and says yes or no; the one thing it remembers is which codes
it has already said yes to.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from urllib.parse import quote

# What every authenticator app assumes when a QR code says nothing else.
STEP = 30
DIGITS = 6
# How many steps either side of now are accepted. One: a phone's clock is
# rarely more than half a minute out, and a code typed at the end of its
# thirty seconds arrives in the next ones.
WINDOW = 1

_lock = threading.Lock()
# secret -> the last counter a code was accepted for. A code is good for up to
# a minute and a half, and without this so is anybody who watched it typed.
# In memory, so a restart forgets it: a code seen in the minute and a half
# before one would work once more. Left so on purpose — remembering across
# restarts means a write to settings.json on every sign-in, and refusing
# everything up to the moment of starting means a panel that will not take a
# right code for its first half minute.
_accepted = {}


def new_secret() -> str:
    """A fresh secret: 160 bits, as base32 — the form an app takes it in."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def normalise(secret) -> str:
    """A secret as it is stored: upper case, with the spaces a person types in taken out."""
    return "".join(str(secret or "").split()).replace("-", "").upper()


def _key(secret: str) -> bytes:
    text = normalise(secret)
    return base64.b32decode(text + "=" * (-len(text) % 8))


def valid_secret(secret) -> bool:
    """Whether this is base32 and long enough to be worth calling a secret."""
    try:
        return len(_key(secret)) >= 10
    except Exception:
        return False


def code_at(secret: str, counter: int) -> str:
    """The code for one step of the clock (RFC 4226, section 5.3)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def code(secret: str, at: float = None) -> str:
    """The code an app would be showing at `at`, now by default."""
    return code_at(secret, int((time.time() if at is None else at) // STEP))


def match(secret: str, entered, at: float = None, window: int = WINDOW):
    """
    The counter the entered code belongs to, or None when it is not a code for
    now or for a step either side of it.

    Says nothing about whether the code has been used before; accept() does.
    """
    digits = "".join(str(entered or "").split())
    if len(digits) != DIGITS or not digits.isascii() or not digits.isdigit():
        return None
    try:
        now = int((time.time() if at is None else at) // STEP)
        found = None
        # Every candidate is compared, whichever one matches: how long this
        # takes should not say which step the code was for.
        for counter in range(now - window, now + window + 1):
            if hmac.compare_digest(code_at(secret, counter), digits):
                found = counter
        return found
    except Exception:
        # A secret that is not base32 has no codes at all.
        return None


def accept(secret: str, entered, at: float = None) -> bool:
    """
    Whether the code is right — and, if it is, uses it up.

    A code is accepted once. The same one a second time is refused, and so is
    any older one: whoever read it over a shoulder, or off a proxy's log, has
    until the next step of the clock to be first, not a minute and a half to
    be second.
    """
    counter = match(secret, entered, at)
    if counter is None:
        return False
    key = normalise(secret)
    with _lock:
        if counter <= _accepted.get(key, -1):
            return False
        # One secret at a time is all there ever is; an old one's record goes.
        _accepted.clear()
        _accepted[key] = counter
    return True


def uri(secret: str, account: str, issuer: str) -> str:
    """
    The otpauth:// address a QR code carries into an authenticator app.

    The issuer is given twice, as the label's prefix and as a parameter: apps
    differ in which of the two they show, and one that shows neither lists the
    entry under a bare user name. A colon is what divides the two halves of the
    label, so one inside either half is taken out: escaped, it is still a
    colon to the app, which then divides the label in the wrong place.
    """
    issuer = " ".join(str(issuer or "").replace(":", " ").split())
    account = " ".join(str(account or "").replace(":", " ").split())
    label = quote(f"{issuer}:{account}" if issuer else account, safe="")
    return (f"otpauth://totp/{label}?secret={normalise(secret)}"
            f"&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def grouped(secret: str) -> str:
    """The secret in fours, for typing in by hand when the QR code cannot be scanned."""
    text = normalise(secret)
    return " ".join(text[i:i + 4] for i in range(0, len(text), 4))
