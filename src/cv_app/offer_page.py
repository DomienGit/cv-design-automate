"""Strona oferty: pobranie (wolno, z cache), ekstrakcja treści, uzupełnienie oferty.

Kolejność:

1. HTML z cache (``<data_dir>/cache/offers/<source>/<numer>.html``), jeśli jest
   świeży (``CV_APP_OFFER_CACHE_DAYS``); inaczej pobranie z sieci, najwyżej jedna
   strona co ``CV_APP_OFFER_FETCH_DELAY`` sekund (domyślnie 2).
2. Ekstrakcja deterministyczna: dane JSON-LD ``JobPosting``, sekcje ``data-test``
   Pracuj.pl i nagłówki typu „Nasze wymagania” / „Your responsibilities” (także
   nagłówki No Fluff Jobs, JustJoin.it i theprotocol.it, np. „Wymagania obowiązkowe”).
3. Gdy z HTML nie da się wyciągnąć wymagań ani obowiązków, ekstrakcja przez AI
   (:func:`cv_app.claude_client.run_claude`) z oczyszczonego tekstu strony.
4. Gdy nic się nie uda, oferta zostaje z danymi z maila i flagą ``incomplete``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field
from sqlmodel import Session

from cv_app.claude_client import ClaudeError, run_claude
from cv_app.config import Settings
from cv_app.models import Offer

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
MAX_TEXT_FOR_AI = 15_000  # znaków tekstu strony w prompcie ekstrakcji
MAX_ITEMS = 25


class FetchError(Exception):
    """Strony oferty nie udało się pobrać."""


class OfferDetails(BaseModel):
    """Treść oferty ze strony (niezależna od portalu)."""

    title: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    language: Literal["pl", "en"] | None = None
    locations: list[str] = Field(default_factory=list, max_length=10)
    requirements: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    responsibilities: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    technologies: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)

    @property
    def has_content(self) -> bool:
        return bool(self.requirements or self.responsibilities)


# --- pobieranie: tempo i cache ---------------------------------------------------------


@dataclass
class RateLimiter:
    """Najwyżej jedno wywołanie co ``delay`` sekund."""

    delay: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.delay - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)
        self._last = self.clock()


class PageCache:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir / "cache" / "offers"
        self.max_age = timedelta(days=settings.offer_cache_days)

    def path(self, source: str, external_id: str) -> Path:
        safe = re.sub(r"[^\w.-]", "_", external_id)
        return self.root / re.sub(r"[^\w-]", "_", source) / f"{safe}.html"

    def get(self, source: str, external_id: str) -> str | None:
        path = self.path(source, external_id)
        if not path.is_file():
            return None
        age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if age > self.max_age:
            return None
        return path.read_text(encoding="utf-8")

    def put(self, source: str, external_id: str, html: str) -> None:
        path = self.path(source, external_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")


Fetcher = Callable[[str, Settings], str]


def _looks_blocked(status: int, html: str) -> bool:
    return status in (403, 429, 503) or "cf-challenge" in html or "Just a moment..." in html


def http_fetch(url: str, settings: Settings) -> str:
    """Pobiera stronę zwykłym zapytaniem HTTP; przy blokadzie próbuje przeglądarką."""
    import httpx

    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "pl,en;q=0.8"},
            timeout=settings.offer_fetch_timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise FetchError(f"Nie udało się pobrać {url}: {exc}") from exc
    if _looks_blocked(response.status_code, response.text):
        return browser_fetch(url, settings)
    if response.status_code >= 400:
        raise FetchError(f"Strona oferty zwróciła kod {response.status_code}: {url}")
    return response.text


def browser_fetch(url: str, settings: Settings) -> str:
    """Pobiera stronę przez Chromium (Playwright), gdy portal blokuje zwykłe zapytania."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from cv_app.render import chromium_launch_args

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**chromium_launch_args(settings))
            try:
                page = browser.new_page(user_agent=USER_AGENT, locale="pl-PL")
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=settings.offer_fetch_timeout * 1000
                )
                if response is not None and response.status >= 400:
                    raise FetchError(f"Strona oferty zwróciła kod {response.status}: {url}")
                return page.content()
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise FetchError(f"Przeglądarka nie pobrała {url}: {exc}") from exc


# --- ekstrakcja z HTML -----------------------------------------------------------------

_SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    "responsibilities": (
        "zakres obowiązków",
        "twój zakres obowiązków",
        "obowiązki",
        "czym będziesz się zajmować",
        "your responsibilities",
        "responsibilities",
        "what you will do",
        "what you'll do",
        "your tasks",
        "tasks",
        "twoje zadania",
        "zadania",
    ),
    "requirements": (
        "nasze wymagania",
        "wymagania",
        "czego oczekujemy",
        "our requirements",
        "requirements",
        "what we expect",
        "qualifications",
        "wymagania obowiązkowe",  # No Fluff Jobs
        "obowiązkowe",
        "must have",
        "must-have",
        "obligatory",
        "requirements: must have",
        "oczekiwania",
        "our expectations",
    ),
    "technologies": (
        "technologie",
        "technologie, których używamy",
        "technologies we use",
        "technologies",
        "tech stack",
        "stack technologiczny",
    ),
}

_OPTIONAL_MARKERS = ("optional", "nice", "mile widziane")


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        item = item.strip(" •-–;")
        key = item.lower()
        if item and key not in seen and len(item) <= 400:
            seen.add(key)
            out.append(item)
    return out[:MAX_ITEMS]


def _list_items(node: Tag) -> list[str]:
    items = [_text(li) for li in node.find_all("li")]
    return [i for i in items if i]


def _from_json_ld(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else data.get("@graph", [data])
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _html_list(value: object) -> list[str]:
    """Pole JSON-LD (tekst albo HTML z listą) → lista punktów."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    fragment = BeautifulSoup(value, "html.parser")
    items = _list_items(fragment)
    if items:
        return items
    return [p.strip() for p in re.split(r"\n+|•", fragment.get_text("\n")) if p.strip()]


def _sections_by_data_test(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Pracuj.pl: ``data-test="section-requirements"`` itd."""
    found: dict[str, list[str]] = {}
    for key in ("responsibilities", "requirements", "technologies"):
        for node in soup.select(f'[data-test*="section-{key}"]'):
            items: list[str] = []
            for li in node.find_all("li"):
                text = _text(li)
                parent_test = " ".join(
                    str(p.get("data-test", "")) for p in li.parents if isinstance(p, Tag)
                ).lower()
                if text and any(m in parent_test for m in _OPTIONAL_MARKERS):
                    text += " (mile widziane)"
                if text:
                    items.append(text)
            if not items and key == "technologies":
                items = [_text(n) for n in node.select('[data-test*="item-technologies"]')]
            if items:
                found.setdefault(key, []).extend(items)
    return found


def _sections_by_heading(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Ogólny portal: lista punktów pod nagłówkiem „Wymagania”, „Responsibilities”..."""
    found: dict[str, list[str]] = {}
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
        label = _text(heading).lower().rstrip(":")
        key = next(
            (k for k, names in _SECTION_HEADINGS.items() if label in names),
            None,
        )
        if key is None or key in found:
            continue
        lst = heading.find_next(["ul", "ol"])
        if lst is not None:
            items = _list_items(lst)
            if items:
                found[key] = items
    return found


_PL_WORDS = re.compile(
    r"\b(i|oraz|w|z|na|do|się|jest|dla|wymagania|obowiązków|doświadczenie|znajomość)\b"
)
_EN_WORDS = re.compile(r"\b(and|the|with|for|of|to|is|you|we|experience|knowledge|skills)\b")


def detect_language(text: str) -> Literal["pl", "en"]:
    low = text.lower()
    pl = len(_PL_WORDS.findall(low)) + 3 * len(re.findall(r"[ąćęłńóśźż]", low)) // 10
    en = len(_EN_WORDS.findall(low))
    return "en" if en > pl * 1.5 else "pl"


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def extract_from_html(html: str) -> OfferDetails:
    """Ekstrakcja bez AI. Zwraca to, co udało się znaleźć (może być puste)."""
    soup = BeautifulSoup(html, "html.parser")
    ld = _from_json_ld(soup)

    sections = _sections_by_heading(soup)
    sections.update(_sections_by_data_test(soup))
    if ld and isinstance(ld.get("description"), str):
        # JustJoin.it i inne: cała treść w JSON-LD ``description`` (HTML z nagłówkami).
        for key, items in _sections_by_heading(
            BeautifulSoup(ld["description"], "html.parser")
        ).items():
            sections.setdefault(key, items)
    if ld:
        sections.setdefault("responsibilities", _html_list(ld.get("responsibilities")))
        sections.setdefault(
            "requirements",
            _html_list(ld.get("qualifications")) or _html_list(ld.get("experienceRequirements")),
        )
        sections.setdefault("technologies", _html_list(ld.get("skills")))

    title_node = soup.select_one('[data-test="text-positionName"]') or soup.find("h1")
    company_node = soup.select_one('[data-test="text-employerName"]')
    org = ld.get("hiringOrganization") if isinstance(ld.get("hiringOrganization"), dict) else {}
    company = _text(company_node) if company_node else org.get("name")
    if company:
        company = re.sub(r"\s*O firmie\s*$", "", company).strip() or None

    locations: list[str] = []
    for place in ld.get("jobLocation", []) if isinstance(ld.get("jobLocation"), list) else []:
        city = (
            (place.get("address") or {}).get("addressLocality") if isinstance(place, dict) else None
        )
        if city:
            locations.append(city)

    body = (
        " ".join(sections.get("requirements", []) + sections.get("responsibilities", []))
        or visible_text(html)[:3000]
    )
    return OfferDetails(
        title=(_text(title_node) if title_node else ld.get("title")) or None,
        company=company,
        language=detect_language(body),
        locations=_dedupe(locations),
        requirements=_dedupe(sections.get("requirements", [])),
        responsibilities=_dedupe(sections.get("responsibilities", [])),
        technologies=_dedupe(sections.get("technologies", [])),
    )


EXTRACTION_PROMPT = """\
Poniżej jest tekst strony z ofertą pracy. Wyciągnij z niego dane oferty.

Zasady:
- Przepisuj punkty z oferty, nie dopisuj niczego, czego w tekście nie ma.
- requirements: wymagania wobec kandydata (punkty „mile widziane” z dopiskiem
  „(mile widziane)”), responsibilities: obowiązki, technologies: nazwy technologii
  i narzędzi wymienione w ofercie.
- language: „pl”, jeśli oferta jest po polsku, „en”, jeśli po angielsku.
- Pomiń benefity, opis firmy, klauzule RODO i stopkę portalu.
- Gdy czegoś nie ma, zostaw pustą listę albo null.

Tekst strony:
<<<
{text}
>>>
"""


def extract_with_ai(html: str, settings: Settings) -> OfferDetails:
    text = visible_text(html)[:MAX_TEXT_FOR_AI]
    return run_claude(EXTRACTION_PROMPT.format(text=text), OfferDetails, settings=settings)


# --- uzupełnianie ofert ------------------------------------------------------------------


def apply_details(offer: Offer, details: OfferDetails) -> Offer:
    """Uzupełnia ofertę danymi ze strony; dane z maila zostają, gdy strona ich nie ma."""
    offer.company = offer.company or details.company
    offer.locations = offer.locations or details.locations
    offer.requirements = details.requirements or offer.requirements
    offer.responsibilities = details.responsibilities or offer.responsibilities
    offer.technologies = details.technologies or offer.technologies
    offer.language = details.language or offer.language or "pl"
    return offer


@dataclass
class EnrichResult:
    complete: list[Offer] = field(default_factory=list)
    incomplete: list[Offer] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)  # offer.id → komunikat
    fetched: int = 0  # ile stron pobrano z sieci (reszta z cache)


class OfferPageService:
    """Pobiera i odczytuje strony ofert. Wstrzykiwalny ``fetcher`` ułatwia testy."""

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: Fetcher | None = None,
        limiter: RateLimiter | None = None,
        use_ai: bool = True,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher or http_fetch
        self.limiter = limiter or RateLimiter(settings.offer_fetch_delay)
        self.cache = PageCache(settings)
        self.use_ai = use_ai
        self.fetched = 0

    def get_html(self, offer: Offer) -> str:
        cached = self.cache.get(offer.source, offer.external_id)
        if cached is not None:
            return cached
        if not offer.url:
            raise FetchError("Oferta nie ma adresu strony.")
        self.limiter.wait()
        html = self.fetcher(offer.url, self.settings)
        self.fetched += 1
        self.cache.put(offer.source, offer.external_id, html)
        return html

    def details(self, offer: Offer) -> OfferDetails:
        html = self.get_html(offer)
        details = extract_from_html(html)
        if not details.has_content and self.use_ai:
            ai = extract_with_ai(html, self.settings)
            details = ai.model_copy(
                update={
                    "company": ai.company or details.company,
                    "title": ai.title or details.title,
                    "language": ai.language or details.language,
                }
            )
        return details

    def enrich(self, offer: Offer) -> Offer:
        """Uzupełnia jedną ofertę; błąd → ``incomplete`` z danymi z maila."""
        try:
            details = self.details(offer)
        except (FetchError, ClaudeError) as exc:
            offer.incomplete = True
            raise FetchError(str(exc)) from exc
        apply_details(offer, details)
        offer.incomplete = not details.has_content
        offer.fetched_at = datetime.now(UTC)
        return offer

    def enrich_all(self, session: Session, offers: list[Offer]) -> EnrichResult:
        result = EnrichResult()
        for offer in offers:
            try:
                self.enrich(offer)
            except FetchError as exc:
                result.errors[offer.id or 0] = str(exc)
            session.add(offer)
            session.commit()  # zapis po każdej ofercie: przerwanie nie gubi postępu
            (result.incomplete if offer.incomplete else result.complete).append(offer)
        result.fetched = self.fetched
        return result
