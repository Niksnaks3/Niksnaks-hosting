"""Internationalization (i18n) support for Niksnaks-Hosting."""

from __future__ import annotations

import builtins
import gettext
import os
import sys

LANGUAGES: dict[str, str] = {
    "system": "System default",
    "en": "English",
    "pl": "Polski",
}

_localedir: str | None = None


def _default_localedir() -> str:
    """Return the default locale directory for the current environment."""
    if os.environ.get("FLATPAK_ID"):
        return "/app/share/locale"
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "share", "locale")
    return os.path.join(sys.prefix, "share", "locale")


def setup_gettext(localedir: str | None = None) -> None:
    """Initialize gettext and install _() into builtins."""
    global _localedir
    if localedir is None:
        localedir = _default_localedir()
    _localedir = localedir

    try:
        gettext.bindtextdomain("niksnaks-hosting", localedir)
        gettext.textdomain("niksnaks-hosting")
    except Exception:
        pass

    builtins._ = gettext.gettext


def set_language(lang_code: str) -> None:
    """Switch the active translation at runtime."""
    if lang_code == "system" or not lang_code:
        os.environ.pop("LANGUAGE", None)
        try:
            gettext.bindtextdomain("niksnaks-hosting", _localedir)
            gettext.textdomain("niksnaks-hosting")
        except Exception:
            pass
        builtins._ = gettext.gettext
    else:
        try:
            translation = gettext.translation("niksnaks-hosting", _localedir, languages=[lang_code])
            builtins._ = translation.gettext
            os.environ["LANGUAGE"] = lang_code
        except Exception:
            os.environ.pop("LANGUAGE", None)
            try:
                gettext.bindtextdomain("niksnaks-hosting", _localedir)
                gettext.textdomain("niksnaks-hosting")
            except Exception:
                pass
            builtins._ = gettext.gettext


setup_gettext()
