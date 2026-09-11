"""The logs page: recent records from the process, filtered by level and text."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse

import applog
import config
from admin.deps import templates, require_auth

logger = logging.getLogger(__name__)
router = APIRouter()

LIMIT_CHOICES = (100, 300, 1000)


@router.get("/logs", response_class=HTMLResponse)
def logs_view(request: Request, level: str = "", q: str = "", limit: int = 300, auto: str = ""):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    if level not in applog.LEVELS:
        level = ""
    if limit not in LIMIT_CHOICES:
        limit = 300

    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "service_name": config.SERVICE_NAME,
            "entries": applog.entries(level=level, query=q, limit=limit),
            "stats": applog.stats(),
            "levels": applog.LEVELS,
            "limit_choices": LIMIT_CHOICES,
            "level": level,
            "q": q,
            "limit": limit,
            "auto": auto == "1",
        },
    )


@router.post("/logs/ack")
def logs_ack(request: Request):
    """Marks the accumulated warnings as read, which clears the dashboard block."""
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    seq = applog.mark_read()
    logger.info(f"Panel: warnings marked as read (up to record #{seq}).")
    return RedirectResponse(url="/", status_code=303)


@router.post("/logs/clear")
def logs_clear(request: Request):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect

    applog.clear()
    logger.info("Log buffer cleared from the web panel.")
    return RedirectResponse(url="/logs", status_code=303)
