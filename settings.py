"""
The settings the web panel owns.

`settings.json` is the source of truth. `.env` matters only for a first run:
its values seed the fields, and from then on everything is edited in the panel
and written back to the json. That way a fresh installation can be configured
entirely through the interface, without opening an editor on the server.

Secrets — the mailbox passwords, the Gotify token — live here too and are shown
masked in the panel. All that stays in .env is what is needed before the panel
can come up: its own login, address and port, and access to 3x-ui.
"""
import json
import logging
import os
import threading

import config
import i18n

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# The keys the panel owns. The order here is also the order they are written in.
MANAGED_KEYS = (
    # General
    "PANEL_LANG", "MAIL_LANG",
    "SERVICE_NAME", "ADMIN_EMAIL", "XUI_SUBSCRIPTION_BASE_URL",
    # Mail
    "IMAP_SERVER", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD",
    "SMTP_SERVER", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
    "POLL_INTERVAL_SECONDS",
    # Registration
    "XUI_INBOUND_IDS", "XUI_FLOW", "LIMIT_GB", "EXPIRE_DAYS",
    "CODEWORD", "REMARK_INCLUDE_NAME",
    # Notifications
    "GOTIFY_URL", "GOTIFY_TOKEN", "GOTIFY_PRIORITY", "GOTIFY_TITLE", "GOTIFY_MESSAGE",
    # App schemes for the "Add to …" buttons in the letter
    "HAPP_URL", "INCY_URL",
    # The version check
    "UPDATE_CHECK_ENABLED", "UPDATE_DISMISSED_VERSION",
    # Housekeeping: the first-run wizard was finished or declined. It has no
    # field on the settings page — only /setup touches it.
    "SETUP_DONE",
)

# These values never reach the page markup in full: a mask goes instead, and
# the real value can be asked for separately with the reveal button.
SECRET_KEYS = frozenset({"IMAP_PASSWORD", "SMTP_PASSWORD", "GOTIFY_TOKEN"})

# Of those, the ones shown as dots and nothing else. Seeing a few characters at
# either end helps when a value has to be told apart from another one — which
# is the case for a token, of which there may be several. A password is worth
# nothing to recognise and something to leak: over a shoulder, in a screenshot,
# on a shared screen. The reveal button is there for when it is really needed.
FULLY_MASKED = frozenset({"IMAP_PASSWORD", "SMTP_PASSWORD"})

# The form sends this in place of a secret the user did not touch.
UNCHANGED = "•••unchanged•••"

# Keys that are not written along with the rest for company: they are stored
# only when set explicitly. For SETUP_DONE that matters — an absent key and a
# false value mean different things. No key: the installation predates the
# wizard, and whether it is configured shows in the settings themselves.
# false: the wizard was deliberately not taken, and the banner should show.
WRITE_ON_DEMAND = frozenset({"SETUP_DONE"})

_lock = threading.RLock()

# The seed values: whatever came out of .env and the defaults in config.py.
# Taken once before the json is read — after that they matter only to keys the
# json does not carry yet (a first run, or a newly added setting).
BASE = {key: getattr(config, key) for key in MANAGED_KEYS}

# The current values, as held in settings.json.
_stored = {}


def _port(value, name):
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError(i18n.t("{name}: the port must be between 1 and 65535", name=name))
    return number


def _coerce(key, value):
    """Coerces a value to the type config expects, or raises ValueError."""
    if key == "XUI_INBOUND_IDS":
        if isinstance(value, str):
            value = [p.strip() for p in value.split(",") if p.strip()]
        if not isinstance(value, (list, tuple)):
            raise ValueError(i18n.t("XUI_INBOUND_IDS must be a list"))
        # An empty list is allowed: on a fresh install there is nothing to pick
        # yet. Registration will then refuse outright and say so.
        ids = [int(item) for item in value]
        # order matters: the panel adds the client down the list and stops at
        # the first failure
        return list(dict.fromkeys(ids))
    if key in ("LIMIT_GB", "EXPIRE_DAYS"):
        number = int(value)
        if number < 0:
            raise ValueError(i18n.t("the value cannot be negative"))
        return number
    if key == "POLL_INTERVAL_SECONDS":
        number = int(value)
        if number < 5:
            raise ValueError(i18n.t("the polling interval cannot be shorter than 5 seconds"))
        return number
    if key in ("PANEL_LANG", "MAIL_LANG"):
        code = str(value or "").strip().lower()
        if code not in i18n.LANGS:
            raise ValueError(f"unknown language: {code!r}")
        return code
    if key == "SERVICE_NAME":
        text = str(value or "").strip()
        if not text:
            raise ValueError(i18n.t("the service name cannot be empty"))
        return text
    if key == "ADMIN_EMAIL":
        address = str(value or "").strip().lower()
        if address and "@" not in address:
            raise ValueError(i18n.t("the administrator address must contain an @"))
        return address
    if key in ("IMAP_SERVER", "SMTP_SERVER"):
        # Empty means the bot simply will not reach for the mailbox, and says
        # as much in the log.
        return str(value or "").strip()
    if key in ("IMAP_USER", "SMTP_USER"):
        return str(value or "").strip()
    if key in SECRET_KEYS:
        # Spaces at the edges are almost always debris from the clipboard.
        return str(value or "").strip()
    if key == "IMAP_PORT":
        return _port(value, "IMAP")
    if key == "SMTP_PORT":
        return _port(value, "SMTP")
    if key in ("HAPP_URL", "INCY_URL"):
        # Empty simply leaves that app's button out of the letter.
        return str(value or "").strip().rstrip("/")
    if key == "XUI_SUBSCRIPTION_BASE_URL":
        # Empty: build it from the panel address, the way config.py used to.
        # Otherwise the subscription link in the letters degenerates to "/token".
        text = str(value or "").strip().rstrip("/")
        return text or f"{config.XUI_URL}/sub"
    if key == "GOTIFY_URL":
        return str(value or "").strip().rstrip("/")
    if key == "GOTIFY_PRIORITY":
        number = int(value)
        if not 0 <= number <= 10:
            raise ValueError(i18n.t("the Gotify priority must be between 0 and 10"))
        return number
    if key in ("GOTIFY_TITLE", "GOTIFY_MESSAGE"):
        text = str(value or "").strip()
        if not text:
            raise ValueError(i18n.t("the notification text cannot be empty"))
        return text
    if key == "XUI_FLOW":
        # An empty string is a valid value: some inbounds do not use flow, and
        # the panel zeroes it out when creating the client anyway.
        return str(value or "").strip()
    if key == "CODEWORD":
        text = str(value).strip()
        if not text:
            raise ValueError(i18n.t("the code word cannot be empty"))
        return text
    if key == "UPDATE_DISMISSED_VERSION":
        # A version string or nothing; it is only ever compared, never shown.
        return str(value or "").strip().lstrip("v")
    if key in ("REMARK_INCLUDE_NAME", "SETUP_DONE", "UPDATE_CHECK_ENABLED"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    raise ValueError(i18n.t("unknown setting: {key}", key=key))


def _apply():
    """Spreads the values across config attributes. Callers read config.X in place."""
    for key in MANAGED_KEYS:
        setattr(config, key, _stored.get(key, BASE[key]))


def _write(values: dict):
    """Writes the json atomically, so an interrupted write leaves no broken file."""
    ordered = {key: values[key] for key in MANAGED_KEYS if key in values}
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, SETTINGS_PATH)


def load():
    """Reads settings.json and applies it. Called when the process starts."""
    global _stored
    with _lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as e:
                logger.error(f"Could not read {SETTINGS_PATH}: {e}. "
                             f"Running on the values from .env.")
                data = {}

        clean = {}
        for key, value in data.items():
            if key not in MANAGED_KEYS:
                logger.warning(f"Setting {key} from settings.json is not managed by the panel; ignoring it.")
                continue
            try:
                clean[key] = _coerce(key, value)
            except (ValueError, TypeError) as e:
                logger.error(f"Setting {key} in settings.json is invalid ({e}); "
                             f"taking the value from .env.")

        _stored = clean
        _apply()
        if clean:
            logger.info(f"Settings read from settings.json: {len(clean)} values.")
        else:
            logger.info("settings.json is empty or missing; running on the values from .env.")
        return dict(_stored)


def save(new_values: dict):
    """
    Saves the values passed in and applies them at once.

    The whole set of keys goes to the file, not only the changed ones: the panel
    owns the settings, and the json should describe the state in full rather
    than depend on what .env happens to hold. A key absent from new_values keeps
    its current value, which is how a secret the user did not touch survives.
    """
    with _lock:
        merged = {}
        for key in MANAGED_KEYS:
            if key in _stored:
                merged[key] = _stored[key]
            elif key not in WRITE_ON_DEMAND:
                merged[key] = BASE[key]

        for key, value in new_values.items():
            if key not in MANAGED_KEYS:
                raise ValueError(i18n.t("unknown setting: {key}", key=key))
            if key in SECRET_KEYS and value == UNCHANGED:
                continue
            merged[key] = _coerce(key, value)

        _write(merged)
        globals()["_stored"] = merged
        _apply()
        logger.info("Settings saved and applied.")
        return dict(merged)


def is_stored(key: str) -> bool:
    """Whether the key is set explicitly in settings.json, as opposed to taken from .env."""
    with _lock:
        return key in _stored


def secret(key: str) -> str:
    """The real value of a secret, for the reveal button on the settings page."""
    if key not in SECRET_KEYS:
        raise ValueError(f"{key} is not a secret")
    with _lock:
        return str(_stored.get(key, BASE[key]) or "")


# A fixed width, so the mask does not give away how long the value is.
MASK_WIDTH = 12


def mask(value: str, full: bool = False) -> str:
    """
    A preview of a secret: a few characters at each end, dots in between.

    A short value is shown as dots alone — with three characters at each end a
    short password is far too easy to recognise. With `full`, so is every value,
    however long, and the dots are always the same number of them.
    """
    text = str(value or "")
    if not text:
        return ""
    if full or len(text) < 12:
        return "•" * MASK_WIDTH
    return f"{text[:4]}{'•' * 8}{text[-4:]}"


def describe():
    """The current state, for the settings page."""
    with _lock:
        out = {}
        for key in MANAGED_KEYS:
            value = getattr(config, key)
            if key in SECRET_KEYS:
                out[key] = {"value": "", "is_secret": True, "isset": bool(value),
                            "preview": mask(value, full=key in FULLY_MASKED)}
            else:
                out[key] = {"value": value, "is_secret": False,
                            "isset": value not in ("", [], None), "preview": ""}
        return out
