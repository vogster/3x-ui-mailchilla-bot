"""
The Russian catalogue is kept complete.

A missing key is not an error anywhere — i18n falls back to the English string —
so it shows up as English text sitting in a Russian panel, and only if somebody
happens to look at that page. This is the pass that looks for all of them at
once, which is exactly the mistake it was written after.
"""
import glob
import os
import re
import unittest

import email_texts
from lang.ru import TEXTS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# t("...") and t('...') in a template or in Python. A string broken across lines
# is not matched, which is why the check errs towards silence rather than noise.
CALL = re.compile(r"""\bt\(\s*(["'])((?:(?!\1).){4,}?)\1\s*[),]""", re.S)


def _missing(strings):
    return sorted({s for s in strings if s and s not in TEXTS})


class LetterEditorCaptions(unittest.TestCase):
    """The captions and hints in GROUPS are interface strings, not letter texts."""

    def test_every_caption_and_hint_is_translated(self):
        wanted = []
        for group in email_texts.GROUPS:
            wanted += [group["title"], group["hint"]]
            for key, label, kind, hint in email_texts.walk(group["fields"]):
                wanted += [label, hint]
        self.assertEqual(_missing(wanted), [])


class Templates(unittest.TestCase):
    def test_every_panel_string_is_translated(self):
        missing = []
        for path in glob.glob(os.path.join(ROOT, "admin", "templates", "*.html")):
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for _, string in CALL.findall(text):
                if string not in TEXTS:
                    missing.append(f"{os.path.basename(path)}: {string}")
        self.assertEqual(sorted(set(missing)), [])


class LetterTexts(unittest.TestCase):
    """Every editable string ships in both languages, or a Russian letter would
    come out half English."""

    def test_no_key_is_missing_from_a_language(self):
        for code, defaults in email_texts.DEFAULTS_BY_LANG.items():
            self.assertEqual(sorted(set(email_texts.ALL_KEYS) - set(defaults)), [],
                             f"missing from {code}")

    def test_no_default_is_for_a_key_that_no_longer_exists(self):
        for code, defaults in email_texts.DEFAULTS_BY_LANG.items():
            self.assertEqual(sorted(set(defaults) - set(email_texts.ALL_KEYS)), [],
                             f"left over in {code}")


if __name__ == "__main__":
    unittest.main()
