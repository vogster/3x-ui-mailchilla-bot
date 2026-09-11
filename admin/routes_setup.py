"""
The first-run setup wizard.

A freshly cloned copy knows neither the mailbox nor the inbounds, and there is
nowhere to guess them from. The wizard walks through the steps and saves each
one as it is passed: closing the tab halfway loses nothing already filled in.

The wizard owns no fields of its own — they are the same settings, laid out in
the order "what is necessary first, what is optional after". So the field names
match the settings page and saving goes through the same settings.save().
"""
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse

import config
import i18n
import settings
from admin.deps import templates, require_auth, setup_status
from xui_client import get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()

# Form field name to settings key.
FIELD_KEYS = {
    "service_name": "SERVICE_NAME",
    "xui_subscription_base_url": "XUI_SUBSCRIPTION_BASE_URL",
    "admin_email": "ADMIN_EMAIL",
    "imap_server": "IMAP_SERVER",
    "imap_port": "IMAP_PORT",
    "imap_user": "IMAP_USER",
    "imap_password": "IMAP_PASSWORD",
    "smtp_server": "SMTP_SERVER",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD",
    "codeword": "CODEWORD",
    "limit_gb": "LIMIT_GB",
    "expire_days": "EXPIRE_DAYS",
    "gotify_url": "GOTIFY_URL",
    "gotify_token": "GOTIFY_TOKEN",
    "happ_url": "HAPP_URL",
    "incy_url": "INCY_URL",
}


def _safe_path(value: str) -> str:
    """
    Where the close button leads. Only our own relative path is accepted: a
    string like "//evil.example" reads to the browser as another site's address
    rather than a path.
    """
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/setup"):
        return "/"
    return value


def _safe_back(request: Request) -> str:
    """
    Where "I will do it myself" returns to — the page it was pressed on.

    Only our own referer counts: a foreign address in that header would turn the
    button into an open redirect.
    """
    referer = request.headers.get("referer") or ""
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return "/"
    path = parsed.path or "/"
    return "/" if path.startswith("/setup") else path


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, back: str = "/"):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    state = settings.describe()
    selected = list(state["XUI_INBOUND_IDS"]["value"])
    inbounds = get_shared_client().get_inbounds()
    for inbound in inbounds:
        inbound["selected"] = inbound["id"] in selected

    return templates.TemplateResponse("setup.html", {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "back": _safe_path(back),
        "unchanged_marker": settings.UNCHANGED,
        "state": state,
        "inbounds": inbounds,
        "panel_unavailable": not inbounds,
        "status": setup_status(),
    })


@router.post("/setup/save")
async def setup_save(request: Request):
    """
    Saves a single step.

    The route is async because which fields arrive depends on the step, so the
    form has to be read whole. Keys absent from the submission keep their
    current value, which is how skipping a step wipes nothing out.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return JSONResponse({"error": i18n.t("You need to sign in")}, status_code=401)

    form = await request.form()
    values = {}
    for field, key in FIELD_KEYS.items():
        if field not in form:
            continue
        value = form[field]
        # An untouched secret arrives as the marker — keep the stored one.
        if key in settings.SECRET_KEYS and value == settings.UNCHANGED:
            continue
        values[key] = value

    # The inbound checkboxes exist only when 3x-ui answered with a list.
    if form.get("inbounds_present"):
        try:
            values["XUI_INBOUND_IDS"] = [int(v) for v in form.getlist("inbound_ids")]
        except ValueError:
            return JSONResponse({"ok": False, "error": i18n.t("The list of inbounds is not valid")},
                                status_code=400)

    if not values:
        return JSONResponse({"ok": True})

    try:
        settings.save(values)
    except (ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        logger.error(f"Wizard: could not write settings.json: {e}")
        return JSONResponse({"ok": False, "error": i18n.t("Could not write the settings file: {error}", error=e)},
                            status_code=500)
    return JSONResponse({"ok": True})


@router.post("/setup/finish")
def setup_finish(request: Request):
    """The wizard is done: the banner stops appearing."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    settings.save({"SETUP_DONE": True})
    logger.info("Panel: first-run setup finished.")
    return RedirectResponse(url="/?setup=done", status_code=303)


@router.post("/setup/dismiss")
def setup_dismiss(request: Request):
    """"I will do it myself": hides the banner without changing any setting."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    settings.save({"SETUP_DONE": True})
    logger.info("Panel: setup wizard dismissed, configuring by hand.")
    return RedirectResponse(url=_safe_back(request), status_code=303)
