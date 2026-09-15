import os
from dotenv import load_dotenv

load_dotenv()

# The project's own name and version — not to be confused with SERVICE_NAME,
# which names one particular installation and goes into the letters.
APP_NAME = "Mailchilla"
APP_VERSION = "0.1.5"
# Where the sidebar's GitHub link points.
PROJECT_URL = "https://github.com/vogster/3x-ui-mailchilla-bot"

XUI_URL = os.getenv("XUI_URL", "http://localhost:2053").rstrip("/")
XUI_USERNAME = os.getenv("XUI_USERNAME", "admin")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "")
XUI_API_TOKEN = os.getenv("XUI_API_TOKEN", "").strip()
# Empty by default: on a fresh install the inbounds are ticked in the panel,
# taken from the real 3x-ui list. Guessing at "the first one" helps nobody.
inbounds_raw = os.getenv("XUI_INBOUND_IDS") or os.getenv("XUI_INBOUND_ID") or ""
XUI_INBOUND_IDS = [int(x.strip()) for x in inbounds_raw.split(",") if x.strip()]
# Set in the web panel (General tab). An empty value means "build it from
# the panel address": {XUI_URL}/sub.
XUI_SUBSCRIPTION_BASE_URL = os.getenv("XUI_SUBSCRIPTION_BASE_URL", f"{XUI_URL}/sub").rstrip("/")
XUI_FLOW = os.getenv("XUI_FLOW", "")

IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.yandex.ru")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Set in the web panel (General tab); it survives in .env only as a fallback
# for installations that run without the panel.
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))

# --- The seed of the first tariff ---
# These three described what every new client got, back when there was one code
# word and one set of limits. They belong to a tariff now (tariffs.json, the
# Tariffs page), and nothing reads them after the first run: tariffs.load()
# builds the opening tariff out of them when no tariffs.json exists yet, which
# is how an installation from before tariffs keeps working without being
# touched. They stay in settings.MANAGED_KEYS for the same reason — a copy
# rolled back to 0.1.x finds them where it left them.
CODEWORD = os.getenv("CODEWORD", "START_VPN").strip()
LIMIT_GB = int(os.getenv("LIMIT_GB", "0"))
EXPIRE_DAYS = int(os.getenv("EXPIRE_DAYS", "30"))
SERVICE_NAME = os.getenv("SERVICE_NAME", APP_NAME).strip() or APP_NAME

# Gotify Configuration
GOTIFY_URL = os.getenv("GOTIFY_URL", "").strip()
# Seeds the panel on a first run; after that the token is edited there.
GOTIFY_TOKEN = os.getenv("GOTIFY_TOKEN", "").strip()
# Templates for the new-registration notification. {email}, {name} and
# {service} are substituted.
GOTIFY_TITLE = os.getenv("GOTIFY_TITLE", "New {service} registration")
GOTIFY_MESSAGE = os.getenv("GOTIFY_MESSAGE", "{email} has been registered.")
try:
    GOTIFY_PRIORITY = int(os.getenv("GOTIFY_PRIORITY", "5"))
except ValueError:
    GOTIFY_PRIORITY = 5

# App Urls Config
# The two schemes are the same for every installation — they belong to the apps,
# not to this server — so they are filled in from the start and the first-run
# wizard has nothing to ask about them. Emptying one in the panel removes its
# button from the letter.
HAPP_URL = os.getenv("HAPP_URL", "happ://add")
INCY_URL = os.getenv("INCY_URL", "incy://add")

# --- Sender name on registration ---
# When True, the sender's name goes into the client's separate `comment` field.
# The `email` field always gets the bare address alone: the panel accepts only
# a limited set of characters there and rejects spaces and angle brackets.
REMARK_INCLUDE_NAME = os.getenv("REMARK_INCLUDE_NAME", "true").strip().lower() in ("1", "true", "yes", "on")

# --- The welcome letter ---
# The QR code of the subscription link. On by default: the reader who opened
# their mail on a computer is exactly who the letter is for.
WELCOME_QR_ENABLED = True

# --- Language ---
# The interface language and the language letters go out in are separate:
# the panel can be English while the letters stay Russian. Both live in the
# web panel; the values here are only the starting point.
PANEL_LANG = os.getenv("PANEL_LANG", "en").strip().lower()
MAIL_LANG = os.getenv("MAIL_LANG", "en").strip().lower()

# --- The version check ---
# Asking GitHub, a few times a day, whether a newer version has been released.
# It is the only request this project makes on its own behalf, and some of the
# people running it are behind exactly the sort of blocking the project exists
# to work around — so it can be switched off in the panel.
UPDATE_CHECK_ENABLED = True
# The version the banner was dismissed for. It comes back when a newer one
# than this is released.
UPDATE_DISMISSED_VERSION = ""

# --- First-run setup ---
# Marks the wizard as done (or declined by the user). It lives in settings.json
# alone; the value here is only the starting point for a fresh copy.
SETUP_DONE = False

# --- Logs ---
# The in-memory ring buffer is what the panel's /logs page reads.
LOG_BUFFER_SIZE = int(os.getenv("LOG_BUFFER_SIZE", "2000"))
# A rotating file so history survives a restart. Empty means write nothing.
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log").strip()
LOG_FILE_MAX_BYTES = int(os.getenv("LOG_FILE_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_FILE_BACKUPS = int(os.getenv("LOG_FILE_BACKUPS", "3"))

# --- Web panel ---
# The panel is always raised: without it there is nothing to configure the
# project with, since every setting lives there. The key is deliberately absent
# from .env.example — it is an emergency switch, not a line you fill in when
# installing. The ordinary way to run the bot alone is `python email_bot.py`.
ADMIN_PANEL_ENABLED = os.getenv("ADMIN_PANEL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
ADMIN_PANEL_USER = os.getenv("ADMIN_PANEL_USER", "admin").strip()
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
# Secret for signing the session cookie. Empty means a random one per start, so sessions do not survive a restart.
ADMIN_PANEL_SECRET = os.getenv("ADMIN_PANEL_SECRET", "").strip()
ADMIN_PANEL_HOST = os.getenv("ADMIN_PANEL_HOST", "127.0.0.1").strip()
ADMIN_PANEL_PORT = int(os.getenv("ADMIN_PANEL_PORT", "8080"))
