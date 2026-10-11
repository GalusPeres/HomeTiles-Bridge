"""Every Home Assistant language of the Bridge has every text of en.json.

The Bridge speaks the panel's languages in Home Assistant: German, English,
French and Polish. A missing text falls back to English, so a language file
must carry the same keys as en.json and the same {placeholders} in each text.
"""

import json
import re
import unittest
from pathlib import Path

TRANSLATIONS = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl" / "translations"
LANGUAGES = ("de", "en", "fr", "pl")


def _flat(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat(value, f"{path}.{key}" if path else key)
    else:
        yield path, node


def _load(language):
    return dict(_flat(json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))))


class TranslationFileTests(unittest.TestCase):
    def test_the_panel_languages_are_there(self):
        found = sorted(path.stem for path in TRANSLATIONS.glob("*.json"))
        self.assertEqual(found, sorted(LANGUAGES))

    def test_every_language_has_every_text_with_its_placeholders(self):
        english = _load("en")
        for language in LANGUAGES:
            texts = _load(language)
            self.assertEqual(sorted(texts), sorted(english), language)
            for path, text in texts.items():
                self.assertIsInstance(text, str, f"{language}: {path}")
                self.assertTrue(text.strip(), f"{language}: {path} is empty")
                self.assertEqual(sorted(re.findall(r"\{[a-z_]+\}", text)),
                                 sorted(re.findall(r"\{[a-z_]+\}", english[path])), f"{language}: {path}")

    def test_panel_buttons_carry_the_panels_own_names(self):
        # The panel's Pair button and Settings path in its own language
        # (firmware src/core/i18n/i18n.cpp), so the steps match the screen.
        for language, pair, path in (("de", "Koppeln", "Einstellungen > System > Sicherheit"),
                                     ("en", "Pair", "Settings > System > Security"),
                                     ("fr", "Appairer", "Paramètres > Système > Sécurité"),
                                     ("pl", "Paruj", "Ustawienia > System > Bezpieczeństwo")):
            texts = _load(language)
            self.assertIn(pair, texts["config.abort.link_press_pair"], language)
            self.assertIn(path, texts["config.abort.link_press_pair"], language)


if __name__ == "__main__":
    unittest.main()
