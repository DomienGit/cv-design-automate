"""Render CV: bloki (JSON) + dane z profilu → HTML (Jinja2) → PDF (Playwright).

Dane kontaktowe i zdjęcie bierzemy lokalnie z profilu i wstawiamy dopiero tutaj.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from cv_app.config import Settings
from cv_app.cv_blocks import CVBlocks, Language
from cv_app.profile import Contact, Profile

TEMPLATE_VERSION = "1"
CV_TEMPLATE_DIR = Path(__file__).parent / "templates" / "cv"
FONTS_DIR = CV_TEMPLATE_DIR / "fonts"

PHOTO_NAMES = ("zdjecie", "zdjęcie", "photo", "foto")
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

LABELS: dict[Language, dict[str, str]] = {
    "pl": {
        "contact": "Kontakt",
        "skills": "Umiejętności",
        "languages": "Języki",
        "summary": "Podsumowanie",
        "projects": "Projekty",
        "education": "Edukacja",
        "clause": (
            "Wyrażam zgodę na przetwarzanie moich danych osobowych dla potrzeb niezbędnych "
            "do realizacji procesu rekrutacji zgodnie z Rozporządzeniem Parlamentu "
            "Europejskiego i Rady (UE) 2016/679 z dnia 27 kwietnia 2016 r. (RODO)."
        ),
    },
    "en": {
        "contact": "Contact",
        "skills": "Skills",
        "languages": "Languages",
        "summary": "Summary",
        "projects": "Projects",
        "education": "Education",
        "clause": (
            "I hereby consent to the processing of my personal data for the purposes "
            "necessary for the recruitment process in accordance with Regulation (EU) "
            "2016/679 of the European Parliament and of the Council of 27 April 2016 (GDPR)."
        ),
    },
}

FONT_FACES = (
    ("Inter", 400, "Inter-Regular.woff2"),
    ("Inter", 500, "Inter-Medium.woff2"),
    ("Inter", 600, "Inter-SemiBold.woff2"),
    ("Inter Display", 700, "InterDisplay-Bold.woff2"),
)


class RenderError(Exception):
    """Nie udało się wyrenderować PDF (np. brak przeglądarki Chromium)."""


@dataclass
class ContactItem:
    icon: str  # phone / mail / pin / code / globe / link
    text: str
    href: str | None = None


@dataclass
class RenderContext:
    name: str
    contact: list[ContactItem] = field(default_factory=list)
    photo_data_uri: str | None = None


def _short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def _city(location: str) -> str:
    """„Ruda Śląska (41-712), Polska” → „Ruda Śląska, Polska” (bez kodu pocztowego)."""
    return re.sub(r"\s*\([^)]*\)", "", location).strip()


def contact_items(contact: Contact) -> list[ContactItem]:
    items: list[ContactItem] = []
    if contact.phone:
        items.append(ContactItem("phone", contact.phone, "tel:" + contact.phone.replace(" ", "")))
    if contact.email:
        items.append(ContactItem("mail", contact.email, "mailto:" + contact.email))
    if contact.location:
        items.append(ContactItem("pin", _city(contact.location)))
    for icon, url in (
        ("code", contact.github),
        ("code", contact.gitlab),
        ("globe", contact.website),
        ("link", contact.linkedin),
    ):
        if url:
            items.append(ContactItem(icon, _short_url(url), url))
    return items


def find_photo(settings: Settings) -> Path | None:
    if settings.photo_path is not None:
        return settings.photo_path if settings.photo_path.is_file() else None
    for name in PHOTO_NAMES:
        for suffix in PHOTO_SUFFIXES:
            candidate = settings.notes_dir / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_context(profile: Profile, settings: Settings) -> RenderContext:
    photo = find_photo(settings) if profile.contact.photo else None
    return RenderContext(
        name=profile.contact.name,
        contact=contact_items(profile.contact),
        photo_data_uri=_data_uri(photo) if photo else None,
    )


@lru_cache
def _font_css() -> str:
    rules = []
    for family, weight, filename in FONT_FACES:
        uri = "data:font/woff2;base64," + base64.b64encode(
            (FONTS_DIR / filename).read_bytes()
        ).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
            f"src:url({uri}) format('woff2');}}"
        )
    return "\n".join(rules)


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(CV_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(blocks: CVBlocks, ctx: RenderContext) -> str:
    env = _env()
    css = (CV_TEMPLATE_DIR / "cv.css").read_text(encoding="utf-8")
    return env.get_template("cv.html").render(
        cv=blocks,
        ctx=ctx,
        t=LABELS[blocks.language],
        lang=blocks.language,
        css=css,
        font_css=_font_css(),
        template_version=TEMPLATE_VERSION,
    )


def chromium_launch_args(settings: Settings) -> dict[str, object]:
    """Argumenty ``chromium.launch``: własna przeglądarka z ustawień, jeśli podana."""
    if settings.chromium_executable is not None:
        return {"executable_path": str(settings.chromium_executable)}
    return {}


def render_pdf(html: str, out_path: Path, settings: Settings) -> Path:
    """Zapisuje PDF A4. Wymaga Chromium dla Playwrighta (``playwright install chromium``)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**chromium_launch_args(settings))
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.emulate_media(media="print")
                page.pdf(
                    path=str(out_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise RenderError(
            "Nie udało się wygenerować PDF. Jeśli brakuje przeglądarki, uruchom: "
            f"uv run playwright install chromium. Szczegóły: {exc}"
        ) from exc
    return out_path
