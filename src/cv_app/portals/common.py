"""Wspólne narzędzia adapterów: odpakowanie linków śledzących, czyszczenie adresów."""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from bs4 import Tag

# Parametry przekierowań spotykane w trackerach kliknięć.
_REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "target", "destination")
_SALARY = re.compile(r"\d.*(zł|pln|eur|€|usd|\$|gbp|£|chf)", re.I)


def clean_url(url: str) -> str:
    """Adres bez parametrów i fragmentu (``utm_*``, ``sendid``, ``criteria``...)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def _b64_url(segment: str) -> str | None:
    """Segment ścieżki zakodowany base64 (wariant URL-safe), jeśli to adres http(s)."""
    if len(segment) < 12 or not re.fullmatch(r"[A-Za-z0-9_\-+/=]+", segment):
        return None
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        text = decoded.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return text if text.startswith(("http://", "https://")) else None


def unwrap_tracking(url: str) -> str:
    """Docelowy adres z linku śledzącego.

    Obsługuje m.in. ``mail-link.nofluffjobs.com/click/<id>/<base64 adresu>/<id>`` oraz
    trackery z adresem w parametrze (``?url=...``). Zwykły link wraca bez zmian.
    """
    parts = urlsplit(url)
    if not parts.scheme.startswith("http"):
        return url
    query = parse_qs(parts.query)
    for name in _REDIRECT_PARAMS:
        for value in query.get(name, []):
            if value.startswith(("http://", "https://")):
                return unwrap_tracking(value)
    for segment in parts.path.split("/"):
        target = _b64_url(segment)
        if target:
            return unwrap_tracking(target)
    return url


def text_of(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def looks_like_salary(text: str) -> bool:
    return bool(_SALARY.search(text))
