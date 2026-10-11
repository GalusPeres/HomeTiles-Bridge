"""Every Home Assistant language of the Bridge has every text of en.json.

Home Assistant shows an integration's texts in the user's language and falls
back to English for a missing file, also for a regional variant (es-419 does
not fall back to es). Each language file must carry the same keys as en.json
and the same {placeholders} in each text.
"""

import json
import re
import unittest
from pathlib import Path

TRANSLATIONS = Path(__file__).resolve().parents[1] / "custom_components" / "tab5_lvgl" / "translations"
# homeassistant/generated/languages.py
HA_LANGUAGES = {
    "af", "ar", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "en-GB", "eo", "es", "es-419",
    "et", "eu", "fa", "fi", "fr", "fy", "ga", "gl", "gsw", "he", "hi", "hr", "hu", "hy", "id", "is", "it",
    "ja", "ka", "ko", "lb", "lt", "lv", "mk", "ml", "nb", "nl", "nn", "pl", "pt", "pt-BR", "ro", "ru", "sk",
    "sl", "sq", "sr", "sr-Latn", "sv", "ta", "te", "th", "tr", "uk", "ur", "vi", "zh-Hans", "zh-Hant",
}
# The panel's own languages (firmware src/core/i18n/i18n.cpp): its Pair button
# and Settings path in that language; every other language shows the panel's
# English names, as the panel then speaks English.
PANEL_NAMES = {
    "de": ("Koppeln", "Einstellungen > System > Sicherheit"),
    "en": ("Pair", "Settings > System > Security"),
    "fr": ("Appairer", "Paramètres > Système > Sécurité"),
    "pl": ("Paruj", "Ustawienia > System > Bezpieczeństwo"),
}


def _flat(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat(value, f"{path}.{key}" if path else key)
    else:
        yield path, node


def _load(language):
    return dict(_flat(json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))))


def _languages():
    return sorted(path.stem for path in TRANSLATIONS.glob("*.json"))


class TranslationFileTests(unittest.TestCase):
    def test_every_file_is_a_home_assistant_language(self):
        self.assertLessEqual(set(_languages()), HA_LANGUAGES)
        self.assertLessEqual(set(PANEL_NAMES), set(_languages()))

    def test_every_language_has_every_text_with_its_placeholders(self):
        english = _load("en")
        for language in _languages():
            texts = _load(language)
            self.assertEqual(sorted(texts), sorted(english), language)
            for path, text in texts.items():
                self.assertIsInstance(text, str, f"{language}: {path}")
                self.assertTrue(text.strip(), f"{language}: {path} is empty")
                self.assertEqual(sorted(re.findall(r"\{[a-z_]+\}", text)),
                                 sorted(re.findall(r"\{[a-z_]+\}", english[path])), f"{language}: {path}")

    def test_panel_buttons_carry_the_names_the_panel_shows(self):
        for language in _languages():
            pair, settings = PANEL_NAMES.get(language, PANEL_NAMES["en"])
            press = _load(language)["config.abort.link_press_pair"]
            self.assertIn(pair, press, language)
            self.assertIn(settings, press, language)


if __name__ == "__main__":
    unittest.main()
