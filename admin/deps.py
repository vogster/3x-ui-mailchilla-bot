"""
Shared panel dependencies: the Jinja2 environment and the auth check.

Split out of ``admin.app`` into its own module to break an import cycle: ``app``
mounts the routers, and the routers need ``templates``/``require_auth``. That
only worked while ``admin.app`` was imported first; any other order — importing
a router from a test, say — died with ImportError.
"""
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import i18n
import settings
import tariffs
import updater

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Needed by every template (the caption in the menu), hence globals rather than
# the context of each route.
templates.env.globals["app_name"] = config.APP_NAME
templates.env.globals["app_version"] = config.APP_VERSION
templates.env.globals["project_url"] = config.PROJECT_URL
# The secret-field script lives in the base template, so the marker is needed on
# every page and cannot be threaded through each route's context.
templates.env.globals["unchanged_marker"] = settings.UNCHANGED
# Every template needs the translator, and most need to know the language for
# the <html lang> attribute.
templates.env.globals["t"] = i18n.t
templates.env.globals["lang_code"] = i18n.panel_lang
templates.env.globals["lang_options"] = i18n.options


def setup_status():
    """
    What is still missing, for the banner shown on every page.

    Computed from values already in memory: this runs on every page render, so
    it must not reach out to 3x-ui. The chosen inbounds are judged by their id
    list alone, without checking them against the panel — the settings page
    does that, where such a request belongs.
    """
    missing = []
    if not (config.IMAP_SERVER and config.IMAP_USER and config.IMAP_PASSWORD):
        missing.append(i18n.t("the mailbox"))
    # A tariff with no inbounds cannot register anybody, and a panel with no
    # tariff at all cannot either. Judged by the ids alone, without asking
    # 3x-ui: this runs on every page render.
    if not any(t["inbound_ids"] for t in tariffs.all_tariffs()):
        missing.append(i18n.t("the inbounds for new clients"))
    if not config.ADMIN_EMAIL:
        missing.append(i18n.t("the administrator address"))
    if settings.is_stored("SETUP_DONE"):
        # The wizard was finished or dismissed by hand — trust what is stored.
        done = bool(config.SETUP_DONE)
    else:
        # No key at all: the installation predates the wizard. Treat it as set
        # up when everything necessary is present — otherwise, after an update,
        # the banner would tell everyone whose project has run for months that
        # it is not configured.
        done = not missing
    return {"done": done, "missing": missing}


templates.env.globals["setup_status"] = setup_status
# Reads only what the background check left in memory, so it costs a page
# render nothing and never reaches the network itself.
templates.env.globals["update_status"] = updater.status


def check_panel_configured():
    """Makes sure a password is set for signing in."""
    if not config.ADMIN_PANEL_PASSWORD:
        raise RuntimeError(
            "ADMIN_PANEL_PASSWORD is not set. Put a password in .env before starting the panel."
        )


def safe_path(target: str, default: str = "/") -> str:
    """
    A local path to redirect to, or the default.

    A leading slash is not enough to make an address local: a browser reads
    "//example.com" and "/\\example.com" as a host of their own, so a redirect
    built from either would leave the panel. Control characters are refused too
    — a newline in a Location header is somebody else's header.
    """
    target = (target or "").strip()
    if not target.startswith("/"):
        return default
    if target.startswith("//") or target.startswith("/\\"):
        return default
    if any(ch in target for ch in "\r\n\t\0"):
        return default
    return target


def is_authenticated(request: Request) -> bool:
    return request.session.get("user") == config.ADMIN_PANEL_USER


def require_auth(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login?next=" + request.url.path, status_code=303)
    return None
