"""The Analytics page: what was sold, how, and whether people stay."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import analytics
import config
import i18n
import payments
import providers
import tariffs
from admin.deps import templates, require_auth
from xui_client import get_shared_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    auth_redirect = require_auth(request)
    if auth_redirect:
        return auth_redirect
    import time
    now_ms = int(time.time() * 1000)
    orders = payments.with_status(payments.APPLIED)
    clients = get_shared_client().get_all_clients()

    days = analytics.revenue_by_day(orders, now_ms)
    peak = max([amount for _, amount in days] + [1])
    split = analytics.breakdown(orders)
    titles = {p.id: p.title for p in providers.ALL}
    return templates.TemplateResponse("stats.html", {
        "request": request,
        "service_name": config.SERVICE_NAME,
        "summary": analytics.summary(orders, now_ms),
        # Bars as a share of the busiest day, for the CSS chart.
        "days": [{"date": d[8:10] + "." + d[5:7], "amount": a, "height": round(100 * a / peak)}
                 for d, a in days],
        "months": analytics.revenue_by_month(orders, now_ms),
        "by_tariff": split["by_tariff"],
        "by_provider": [(titles.get(p, p), a) for p, a in split["by_provider"]],
        "kinds": split["kinds"],
        "conversion": analytics.conversion(orders, tariffs.all_codes()),
        # Without the client list the retention figures would be guesses; the
        # block says so instead.
        "retention": analytics.retention(orders, clients, now_ms) if clients is not None else None,
    })
