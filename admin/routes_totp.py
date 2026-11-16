"""
The page that switches the second step of signing in on and off.

A page of its own rather than a panel on the settings form, for two reasons.
The form is one <form> with one Save, and a code typed into it would be posted
along with the mailbox passwords and every other field; and what happens here
is a conversation — show a secret, take a code back — rather than a value to
be saved.

Both directions ask for a code. Switching on, because a secret is only worth
keeping once the app has shown it holds the same one. Switching off, because
the panel is then one password from anybody again, and a session left open on
somebody else's computer should not be able to arrange that.
"""
import base64
import logging
import secrets

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

import config
import i18n
import templates as mail_templates
import totp
from admin import twostep
from admin.deps import templates, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()

# Where a session keeps the id its secret-in-waiting is filed under. The id is
# what goes into the cookie, never the secret: the cookie is signed, not
# sealed, and anybody holding it can read what is in it.
SETUP_KEY = "two_step_setup"


def _setup_id(request: Request, make: bool = False) -> str:
    """
    This session's setup id, made on the first look at the page.

    It is what keeps one session's secret from another's — see twostep.pending.
    Signing in clears the session, so a new sign-in never inherits one.
    """
    owner = request.session.get(SETUP_KEY)
    if isinstance(owner, str) and owner:
        return owner
    if not make:
        return ""
    owner = secrets.token_urlsafe(16)
    request.session[SETUP_KEY] = owner
    return owner


def _page(request: Request, error: str = "", done: str = "", status_code: int = 200):
    context = {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "on": twostep.on(),
        "error": error,
        "done": done if done in ("on", "off") else "",
    }
    if not context["on"]:
        secret = twostep.pending(_setup_id(request, make=True))
        address = totp.uri(secret, config.ADMIN_PANEL_USER, config.SERVICE_NAME)
        png = mail_templates.qr_png(address)
        context.update(
            # In the page itself, as a data: URI — the picture is the secret,
            # and a URL of its own would be a second request that could be
            # cached, logged or opened by itself.
            qr="data:image/png;base64," + base64.b64encode(png).decode("ascii") if png else "",
            secret=totp.grouped(secret),
            account=config.ADMIN_PANEL_USER,
        )
    response = templates.TemplateResponse("totp.html", context, status_code=status_code)
    # The page holds the secret while it is being set up; it is not for a
    # cache, nor for the back button of whoever sits down next.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/settings/totp", response_class=HTMLResponse)
def totp_page(request: Request, done: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    return _page(request, done=done)


@router.post("/settings/totp/confirm")
def totp_confirm(request: Request, code: str = Form("")):
    """Switches the second step on, once the app has produced a code from the secret shown."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if twostep.on():
        # A second tab, or the form posted twice: it is on already.
        return RedirectResponse(url="/settings/totp", status_code=303)
    try:
        confirmed = twostep.confirm(_setup_id(request), code)
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Could not write the two-step secret to settings.json: {e}")
        return _page(request, status_code=500,
                     error=i18n.t("Could not write the settings file: {error}", error=e))
    if not confirmed:
        return _page(request, status_code=400, error=i18n.t(
            "That is not the code. Check that the app took the QR code on this page — "
            "it changes if the page is left open for long — and that the phone's clock is right."))
    request.session.pop(SETUP_KEY, None)
    logger.info(f"Panel: two-step sign-in switched on by {request.session.get('user')}.")
    return RedirectResponse(url="/settings/totp?done=on", status_code=303)


@router.post("/settings/totp/cancel")
def totp_cancel(request: Request):
    """Calls the setting up off; the secret that was shown is thrown away."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    twostep.drop_pending(_setup_id(request))
    request.session.pop(SETUP_KEY, None)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/totp/disable")
def totp_disable(request: Request, code: str = Form("")):
    """Takes the second step off, for a code — never for a session alone."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if not twostep.on():
        return RedirectResponse(url="/settings/totp", status_code=303)
    # Counted apart from the sign-in form's wrong codes: see twostep.SWITCH_OFF.
    verdict = twostep.check(code, twostep.SWITCH_OFF)
    if verdict == twostep.LOCKED:
        return _page(request, status_code=429,
                     error=i18n.t("Too many wrong codes. Wait a few minutes and try again."))
    if verdict != twostep.OK:
        logger.warning(f"Panel: a wrong code was given to switch two-step sign-in off "
                       f"(user {request.session.get('user')}).")
        return _page(request, status_code=400, error=i18n.t(
            "That is not the code, or it has been used already — a code works once. "
            "Wait for the next one."))
    try:
        twostep.switch_off()
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Could not write settings.json: {e}")
        return _page(request, status_code=500,
                     error=i18n.t("Could not write the settings file: {error}", error=e))
    logger.warning(f"Panel: two-step sign-in switched off by {request.session.get('user')}.")
    return RedirectResponse(url="/settings/totp?done=off", status_code=303)
