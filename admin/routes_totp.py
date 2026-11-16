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

«Set up again» is the two in one, for a new phone or a secret thought to have
been seen: a code for the secret in force buys a new QR code, and the old
secret goes on working until the new one has produced a code of its own.
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
    on = twostep.on()
    owner = _setup_id(request, make=not on)
    replacing = on and twostep.replacing(owner)
    context = {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "on": on,
        # Setting a new secret up beside the one in force: the page then shows
        # the QR code although the second step is on.
        "replacing": replacing,
        "error": error,
        "done": done if done in ("on", "off", "new") else "",
    }
    if not on or replacing:
        secret = twostep.pending(owner)
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
    response = templates.TemplateResponse(request, "totp.html", context, status_code=status_code)
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
    """
    Switches the second step on — or over to a new secret — once the app has
    produced a code from the secret shown.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    owner = _setup_id(request)
    replacing = twostep.replacing(owner)
    if twostep.on() and not replacing:
        # A second tab, or the form posted twice: it is on already. Also a
        # replacement left so long that its secret lapsed.
        return RedirectResponse(url="/settings/totp", status_code=303)
    try:
        confirmed = twostep.confirm(owner, code)
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Could not write the two-step secret to settings.json: {e}")
        return _page(request, status_code=500,
                     error=i18n.t("Could not write the settings file: {error}", error=e))
    if not confirmed:
        return _page(request, status_code=400, error=i18n.t(
            "That is not the code. Check that the app took the QR code on this page — "
            "it changes if the page is left open for long — and that the phone's clock is right."))
    request.session.pop(SETUP_KEY, None)
    if replacing:
        logger.warning(f"Panel: the two-step secret was replaced by {request.session.get('user')}; "
                       f"codes from the old one no longer work.")
        return RedirectResponse(url="/settings/totp?done=new", status_code=303)
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
    # A replacement called off leaves the second step as it was, and the page
    # to come back to is its own.
    return RedirectResponse(url="/settings/totp" if twostep.on() else "/settings",
                            status_code=303)


def _code_refused(request: Request, code: str, what: str):
    """
    The page to answer with when the code for the secret in force is not
    right, or None when it is. Shared by the two things that take one.
    """
    # Counted apart from the sign-in form's wrong codes: see twostep.SWITCH_OFF.
    verdict = twostep.check(code, twostep.SWITCH_OFF)
    if verdict == twostep.OK:
        return None
    if verdict == twostep.LOCKED:
        return _page(request, status_code=429,
                     error=i18n.t("Too many wrong codes. Wait a few minutes and try again."))
    logger.warning(f"Panel: a wrong code was given to {what} "
                   f"(user {request.session.get('user')}).")
    return _page(request, status_code=400, error=i18n.t(
        "That is not the code, or it has been used already — a code works once. "
        "Wait for the next one."))


@router.post("/settings/totp/replace")
def totp_replace(request: Request, code: str = Form("")):
    """
    Starts setting up a new secret, for a code from the one in force.

    Nothing changes yet: the old secret goes on being the second step until
    /settings/totp/confirm has seen a code from the new one.
    """
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if not twostep.on():
        return RedirectResponse(url="/settings/totp", status_code=303)
    refused = _code_refused(request, code, "set two-step sign-in up again")
    if refused:
        return refused
    twostep.begin_replacing(_setup_id(request, make=True))
    logger.info(f"Panel: {request.session.get('user')} is setting two-step sign-in up again; "
                f"the secret in force stays until the new one is confirmed.")
    return RedirectResponse(url="/settings/totp", status_code=303)


@router.post("/settings/totp/disable")
def totp_disable(request: Request, code: str = Form("")):
    """Takes the second step off, for a code — never for a session alone."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    if not twostep.on():
        return RedirectResponse(url="/settings/totp", status_code=303)
    refused = _code_refused(request, code, "switch two-step sign-in off")
    if refused:
        return refused
    try:
        twostep.switch_off()
    except (ValueError, TypeError, OSError) as e:
        logger.error(f"Could not write settings.json: {e}")
        return _page(request, status_code=500,
                     error=i18n.t("Could not write the settings file: {error}", error=e))
    logger.warning(f"Panel: two-step sign-in switched off by {request.session.get('user')}.")
    return RedirectResponse(url="/settings/totp?done=off", status_code=303)
