"""
Building the letters.

The templates live in email_templates and are rendered by Jinja2 — the same one
the web panel runs on. This used to be substitution through str.replace, which
meant any text with curly braces or markup went into the letter as it stood;
Jinja escapes values itself.

Every letter is built in two forms, HTML and plain text. A letter without a text
part is a well-known signal to spam filters, and in a client with HTML turned
off the reader would not even see the subscription link.
"""
import logging
import os
import re
from collections import namedtuple
from datetime import datetime

from markupsafe import Markup, escape
from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
import email_texts

logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_templates")

# .html is escaped, .txt is not: there is no markup there, and "&amp;" in a
# plain-text letter would look like breakage.
_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

GB = 1024 * 1024 * 1024

Email = namedtuple("Email", ["html", "text"])

# The stripe colour beside the broadcast text. Empty means no stripe.
BROADCAST_KINDS = {
    "plain": {"text_key": "broadcast.kind_plain", "color": ""},
    "info": {"text_key": "broadcast.kind_info", "color": "#45b866"},
    "warning": {"text_key": "broadcast.kind_warning", "color": "#d0a215"},
    "urgent": {"text_key": "broadcast.kind_urgent", "color": "#ef5350"},
}
DEFAULT_KIND = "plain"


class _SafeFormat(dict):
    """An unknown substitution stays in the text rather than breaking the build."""

    def __missing__(self, key):
        return "{" + key + "}"


def text(key: str, **values) -> str:
    """A string from the editable texts, with its values substituted in."""
    values.setdefault("service", config.SERVICE_NAME)
    raw = email_texts.get(key)
    try:
        return raw.format_map(_SafeFormat(values))
    except (ValueError, IndexError) as e:
        logger.warning(f"Could not parse text {key!r}: {e}")
        return raw


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def _emph(value) -> Markup:
    """
    Escapes the text, turns **a fragment** bold and line breaks into <br>. That
    is the only markup allowed into a letter: a translation should stay text
    rather than become HTML.
    """
    safe = str(escape(value if value is not None else ""))
    safe = _BOLD.sub(r'<b style="color: #ededef;">\1</b>', safe)
    return Markup(safe.replace("\n", "<br>"))


def _plain(value) -> str:
    """The same for the plain-text version: the asterisks simply come off."""
    return _BOLD.sub(r"\1", str(value if value is not None else ""))


def _split(value):
    """The non-empty lines of a multi-line field, for lists and paragraphs."""
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


_env.globals["t"] = text
_env.globals["split"] = _split
_env.filters["emph"] = _emph
_env.filters["plain"] = _plain


def _render(name: str, **context) -> Email:
    """Builds the HTML and text pair from the templates of the same name."""
    context.setdefault("service_name", config.SERVICE_NAME)
    try:
        html = _env.get_template(f"{name}.html").render(**context)
    except Exception as e:
        logger.error(f"Failed to build the HTML letter {name}: {e}", exc_info=True)
        html = f"<p>{context.get('title', '')}</p>"
    try:
        text = _env.get_template(f"txt/{name}.txt").render(**context)
    except Exception as e:
        logger.error(f"Failed to build the text letter {name}: {e}", exc_info=True)
        text = context.get("title", "")
    return Email(html=html, text=text)


def _app_links(sub_url):
    """The apps that have a scheme set. An empty scheme means no button."""
    apps = []
    for label, scheme in (("Happ", config.HAPP_URL), ("Incy", config.INCY_URL)):
        scheme = (scheme or "").strip().rstrip("/")
        if scheme:
            apps.append({"label": label, "url": f"{scheme}/{sub_url}"})
    return apps


def _fmt_gb(value_bytes) -> str:
    return text("common.unit_gb", gb=round(value_bytes / GB, 2))


def welcome_subject(renewed=False) -> str:
    return text("welcome.subject_again" if renewed else "welcome.subject_new")


def get_welcome_email(sub_url, expire_days, limit_gb, renewed=False) -> Email:
    """The letter carrying the subscription link, on registration or resent."""
    return _render(
        "welcome",
        title=welcome_subject(renewed),
        renewed=renewed,
        sub_url=sub_url,
        apps=[{"label": a["label"],
               "url": a["url"],
               "button": text("welcome.button_app", app=a["label"])}
              for a in _app_links(sub_url)],
        expire_text=(text("welcome.value_forever") if not expire_days
                     else text("welcome.value_days", days=expire_days)),
        limit_text=(text("welcome.value_unlimited") if not limit_gb
                    else text("welcome.value_gb", gb=limit_gb)),
    )


def get_status_email(email, is_active, up, down, total, expiry_time_ms) -> Email:
    """The letter with the subscription status and traffic spent."""
    used = (up or 0) + (down or 0)
    percent = None
    if total and total > 0:
        percent = min(round(used / total * 100, 1), 100)
        used_text = text("status.used_of", used=_fmt_gb(used), total=_fmt_gb(total))
    else:
        used_text = text("status.used_nolimit", used=_fmt_gb(used))

    if expiry_time_ms and expiry_time_ms > 0:
        expiry_text = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y %H:%M")
    else:
        expiry_text = text("status.value_forever")

    return _render(
        "status",
        title=text("status.subject"),
        email=email,
        is_active=bool(is_active),
        status_text=text("status.value_active" if is_active else "status.value_inactive"),
        expiry_text=expiry_text,
        used_text=used_text,
        percent=percent,
        meter_text=text("status.meter_text", percent=percent) if percent is not None else "",
    )


def _howto_steps():
    """
    Parses the multi-line instructions field: a line without indentation is a
    step, an indented one is a note under the step above it.
    """
    steps = []
    for raw in str(email_texts.get("help.howto_steps") or "").splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  ") and steps:
            steps[-1]["notes"].append(raw.strip())
        else:
            steps.append({"title": raw.strip(), "notes": []})
    return steps


def get_help_email(email, sub_url=None) -> Email:
    """Setup instructions and the list of commands."""
    return _render(
        "help",
        title=text("help.subject"),
        email=email,
        sub_url=sub_url,
        steps=_howto_steps(),
    )


def get_broadcast_email(subject, message_body, kind=DEFAULT_KIND) -> Email:
    """A broadcast, or a personal letter from the administrator."""
    accent = BROADCAST_KINDS.get(kind, BROADCAST_KINDS[DEFAULT_KIND])["color"]
    return _render("broadcast", title=subject, body=message_body or "", accent=accent)


def broadcast_kind_options():
    """The broadcast kinds, for the dropdown in the panel."""
    return [{"id": kind, "label": text(data["text_key"]), "color": data["color"]}
            for kind, data in BROADCAST_KINDS.items()]


# Service messages: each has its own keys for subject, text and, where needed, a list.
NOTICE_KINDS = ("unknown", "not_registered", "status_error",
                "create_error", "broadcast_done", "broadcast_empty")


def notice_subject(kind: str) -> str:
    return text(f"notice.{kind}_subject")


def get_notice(kind: str, code_text=None, **values) -> Email:
    """
    A service letter by identifier: the subject, paragraphs and list all come
    from the editable texts, so those can be translated too.
    """
    bullets_key = f"notice.{kind}_bullets"
    bullets = (_split(text(bullets_key, **values))
               if bullets_key in email_texts.ALL_KEYS else [])
    return _render(
        "notice",
        title=notice_subject(kind),
        paragraphs=_split(text(f"notice.{kind}_text", **values)),
        bullets=bullets,
        code_text=code_text,
    )


def get_notice_email(title, paragraphs=None, bullets=None, code_text=None) -> Email:
    """
    A service message: an error, a refusal, a report to the administrator.

    The text is passed as a list of paragraphs and items rather than as ready
    markup, so the template escapes the values and a user's subject line cannot
    drag HTML into the reply.
    """
    return _render(
        "notice",
        title=title,
        paragraphs=paragraphs or [],
        bullets=bullets or [],
        code_text=code_text,
    )
