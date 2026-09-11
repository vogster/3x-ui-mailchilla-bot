"""
The language of the panel interface.

The catalogue is keyed by the English string itself: a template says
``{{ t("Clients") }}`` and English needs no catalogue at all, because a key
that is missing falls back to the key. That is also why English is the
default — there is nothing to be missing.

``lang/ru.py`` holds the Russian, as one flat dict of English -> Russian. Add
a language by dropping ``lang/<code>.py`` beside it with the same shape and
listing the code in LANGS.

The panel language and the language letters are written in are separate
settings: the interface can be English while the letters go out in Russian.
"""
import importlib
import logging
import re

import config

logger = logging.getLogger(__name__)

# Codes the panel offers, with the name each is shown under. The name is
# deliberately in its own language: somebody who cannot read the current one
# still has to be able to find theirs.
LANGS = {"en": "English", "ru": "Русский"}
DEFAULT = "en"

_catalogues = {}


def _catalogue(lang: str) -> dict:
    """The dict for a language, loaded once and kept."""
    if lang in _catalogues:
        return _catalogues[lang]
    if lang == DEFAULT:
        _catalogues[lang] = {}
        return _catalogues[lang]
    try:
        module = importlib.import_module(f"lang.{lang}")
        _catalogues[lang] = dict(module.TEXTS)
    except Exception as e:
        logger.error(f"Could not load the {lang} catalogue: {e}. Falling back to English.")
        _catalogues[lang] = {}
    return _catalogues[lang]


def normalise(lang: str) -> str:
    """A supported code, or the default."""
    code = (lang or "").strip().lower()
    return code if code in LANGS else DEFAULT


def panel_lang() -> str:
    return normalise(getattr(config, "PANEL_LANG", DEFAULT))


def mail_lang() -> str:
    return normalise(getattr(config, "MAIL_LANG", DEFAULT))


# One English word can need several translations: "Active" is one word about a
# single client and another about a column of them. A key may therefore carry a
# context in trailing brackets — t("Active [badge]") — which the catalogue keys
# on and English drops.
_CONTEXT = re.compile(r"\s*\[[^\[\]]+\]$")


def t(text: str, **values) -> str:
    """
    The translation of an English string, or the string itself.

    Named placeholders are filled in only when values are passed, so a text
    that happens to contain braces is left alone.
    """
    out = _catalogue(panel_lang()).get(text)
    if out is None:
        out = _CONTEXT.sub("", text)
    if values:
        try:
            return out.format(**values)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"Could not fill in the translation of {text!r}: {e}")
            return out
    return out


def options(current: str = None):
    """
    Languages for a picker: code, name, and whether it is the current one.

    Without an argument the current one is the panel's; the letters' picker
    passes its own, since the two are set apart from each other.
    """
    current = normalise(current) if current else panel_lang()
    return [{"code": c, "name": n, "current": c == current} for c, n in LANGS.items()]
