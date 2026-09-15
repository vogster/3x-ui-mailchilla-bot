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
import tariffs
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
    "xui_flow": "XUI_FLOW",
    "gotify_url": "GOTIFY_URL",
    "gotify_token": "GOTIFY_TOKEN",
    "happ_url": "HAPP_URL",
    "incy_url": "INCY_URL",
}

# The fields of the opening tariff. They travel in the same step as the rest,
# but land in tariffs.json rather than in the settings: what a client gets
# belongs to a tariff, and on a first run there is exactly one.
TARIFF_FIELDS = ("codeword", "limit_gb", "expire_days")


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


def _first_tariff() -> dict:
    """
    The tariff the wizard fills in — the one tariffs.load() seeded, or a blank
    one when somebody deleted every tariff and came back to the wizard.
    """
    known = tariffs.all_tariffs()
    if known:
        return known[0]
    return {"id": "", "name": i18n.t("Basic [tariff]"), "limit_gb": config.LIMIT_GB,
            "expire_days": config.EXPIRE_DAYS, "inbound_ids": []}


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, back: str = "/"):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    state = settings.describe()
    first = _first_tariff()
    selected = list(first["inbound_ids"])
    inbounds = get_shared_client().get_inbounds()
    for inbound in inbounds:
        inbound["selected"] = inbound["id"] in selected

    words = [c["word"] for c in tariffs.codes_for(first["id"])]
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "back": _safe_path(back),
        "unchanged_marker": settings.UNCHANGED,
        "state": state,
        "tariff": first,
        "tariff_word": words[0] if words else "",
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

    # The step that describes what a client gets writes the opening tariff, not
    # the settings. The inbound checkboxes exist only when 3x-ui answered with a
    # list, and the word, the limit and the term travel with them.
    touches_tariff = form.get("inbounds_present") or any(f in form for f in TARIFF_FIELDS)
    if touches_tariff:
        first = _first_tariff()
        values_tariff = dict(first)
        if form.get("inbounds_present"):
            try:
                values_tariff["inbound_ids"] = [int(v) for v in form.getlist("inbound_ids")]
            except ValueError:
                return JSONResponse({"ok": False, "error": i18n.t("The list of inbounds is not valid")},
                                    status_code=400)
        if "limit_gb" in form:
            values_tariff["limit_gb"] = form["limit_gb"] or 0
        if "expire_days" in form:
            values_tariff["expire_days"] = form["expire_days"] or 0
        was_named = first.get("name", "")
        try:
            saved = tariffs.save_tariff(values_tariff)
            if was_named and was_named != saved["name"]:
                get_shared_client().rename_group(was_named, saved["name"])
            # The word travels in the same step, but it is a code of its own:
            # the wizard is setting up the one way in that a fresh installation
            # needs, and more can be added later on the Tariffs page.
            if "codeword" in form:
                word = str(form["codeword"] or "").strip()
                existing = tariffs.codes_for(saved["id"])
                if word:
                    tariffs.save_code({
                        "word": word,
                        "tariff_id": saved["id"],
                        # The wizard's word is the public one: no limit on it.
                        "uses_left": None,
                        "note": existing[0]["note"] if existing else "",
                        "enabled": True,
                    }, was=existing[0]["word"] if existing else None)
                elif existing:
                    tariffs.delete_code(existing[0]["word"])
        except (ValueError, TypeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except OSError as e:
            logger.error(f"Wizard: could not write tariffs.json: {e}")
            return JSONResponse({"ok": False, "error": i18n.t("Could not write the settings file: {error}", error=e)},
                                status_code=500)

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
