import json
import os
import logging
import markupsafe
from flask import session, request

logger = logging.getLogger(__name__)


class Translations:
    _store: dict[str, dict[str, str]] = {}

    @classmethod
    def set_lang(cls, lang: str, mapping: dict[str, str]) -> None:
        cls._store[lang] = mapping

    @classmethod
    def get(cls, lang: str) -> dict[str, str]:
        return cls._store.get(lang, {})


def init_translations(app):
    """Load translation JSON files from webapp/translations/ directory."""
    translations_dir = os.path.join(app.root_path, "translations")

    for lang in ["en", "pt"]:
        file_path = os.path.join(translations_dir, f"{lang}.json")
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    Translations.set_lang(lang, json.load(f))
                logger.info(f"Loaded translations for language: {lang}")
            else:
                logger.warning(f"Translation file not found: {file_path}")
        except Exception as e:
            logger.error(f"Error loading translation file {file_path}: {e}")

def get_locale():
    """Retrieve the preferred language from session, query params, or accept headers."""
    lang = request.args.get("lang")
    if lang in ["en", "pt"]:
        session["lang"] = lang
        return lang

    if "lang" in session:
        return session["lang"]

    accept_languages = request.headers.get("Accept-Language", "")
    if "pt" in accept_languages.lower() or "br" in accept_languages.lower():
        return "pt"

    return "en"

def translate(key, **kwargs):
    """
    Look up a key in the translation dictionary for the current locale.
    Format any provided keyword arguments in the translation string.
    """
    locale = get_locale()

    translations_map = Translations.get(locale)
    if key not in translations_map:
        translations_map = Translations.get("en")

    translation_str = translations_map.get(key, key)

    if kwargs:
        try:
            escaped_kwargs = {k: markupsafe.escape(str(v)) for k, v in kwargs.items()}
            return translation_str.format(**escaped_kwargs)
        except KeyError as e:
            logger.warning(f"KeyError formatting translation key '{key}': missing placeholder '{e}'")
        except Exception as e:
            logger.error(f"Error formatting translation key '{key}': {e}")

    return translation_str
