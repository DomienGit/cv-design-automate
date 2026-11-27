"""Filtr trafności i deduplikacja ofert z maila (przed pobieraniem stron ofert).

Filtr jest deterministyczny (bez AI) i patrzy na tytuł oferty z maila:

- kategoria: administrator IT, DevOps, helpdesk, backend z Pythonem, AI/ML; to tylko
  etykieta w panelu, oferta bez kategorii też przechodzi (maile zawierają oferty
  z wyszukiwań, które Dominik ustawił w portalach, więc są już przefiltrowane),
- słowa wykluczające poziom (np. „senior”, „lead”) z ustawień
  ``CV_APP_RELEVANCE_EXCLUDE_KEYWORDS``,
- technologie, których według profilu nie wolno wpisywać do CV (reguła
  „Azure: brak doświadczenia” w ``O mnie.md``).

Odrzucone oferty zapisujemy ze statusem ``rejected`` i powodem, żeby panel mógł je
pokazać w osobnej sekcji. Duplikaty (ten sam numer oferty, ta sama oferta wystawiona
ponownie pod nowym numerem albo ta sama oferta na innym portalu) nie trafiają do bazy
drugi raz. Ofertę z różnych portali rozpoznajemy po tytule i firmie, bez dopisków
o płci („(k/m/n)”, „/-ka”) i form prawnych („Sp. z o.o.”, „S.A.”).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from cv_app.config import Settings
from cv_app.models import Offer, OfferStatus
from cv_app.portals.base import MailOffer
from cv_app.profile import Profile

# Kategorie w kolejności ważności: pierwsza pasująca wygrywa
# („Python Developer (AI)” → ai, „Junior DevOps / Administrator” → devops).
CATEGORIES: dict[str, tuple[str, ...]] = {
    "devops": (
        "devops",
        "dev ops",
        "sre",
        "site reliability",
        "cloud",
        "chmur",
        "platform engineer",
        "infrastructure engineer",
        "inzynier infrastruktury",
        "terraform",
        "kubernetes",
        "release engineer",
        "ci/cd",
    ),
    "ai": (
        "ai",
        "a.i.",
        "sztucznej inteligencji",
        "sztuczna inteligencja",
        "machine learning",
        "uczenia maszynowego",
        "ml",
        "mlops",
        "llm",
        "genai",
        "prompt",
        "nlp",
        "rpa",
    ),
    "python": (
        "python",
        "backend",
        "back-end",
        "back end",
        "django",
        "fastapi",
        "flask",
    ),
    "helpdesk": (
        "helpdesk",
        "help desk",
        "service desk",
        "servicedesk",
        "it support",
        "technical support",
        "desktop support",
        "support engineer",
        "wsparcie it",
        "wsparcia it",
        "wsparcie techniczne",
        "wsparcia technicznego",
        "wsparcie uzytkownik",
        "wsparcia uzytkownik",
        "pierwszej linii",
        "first line",
        "1st line",
        "technik informatyk",
        "serwisant komputer",
        "serwisant sprzetu",
        "l1",
        "l2",
    ),
    # Samo „administrator” nie wystarcza (administrator nieruchomości, administracja
    # biura), więc tu tylko połączenia jednoznacznie informatyczne.
    "admin": (
        "sysadmin",
        "administrator it",
        "administrator systemow",
        "administrator systemu",
        "administrator sieci",
        "administrator serwer",
        "administrator baz",
        "administrator aplikacji",
        "administrator infrastruktury",
        "administrator linux",
        "administrator windows",
        "administrator microsoft",
        "administrator m365",
        "administrator ad",
        "it administrator",
        "system administrator",
        "systems administrator",
        "network administrator",
        "linux",
        "unix",
        "system engineer",
        "systems engineer",
        "inzynier systemow",
        "network",
        "sieci komputerow",
        "noc",
        "informatyk",
        "it",
    ),
}

CATEGORY_LABELS = {
    "admin": "Administrator IT",
    "devops": "DevOps",
    "helpdesk": "Helpdesk",
    "python": "Backend z Pythonem",
    "ai": "AI / ML",
}

REPOST_WINDOW = timedelta(days=30)


def normalize(text: str) -> str:
    """Małe litery, bez polskich znaków i nadmiarowych spacji („Systemów” → „systemow”)."""
    text = text.lower().replace("ł", "l")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.split())


def _contains(text: str, keyword: str) -> bool:
    """Słowo kluczowe jako osobne słowo albo początek słowa („chmur” → „chmurowy”)."""
    kw = normalize(keyword)
    return re.search(rf"(?<![\w]){re.escape(kw)}", text) is not None and (
        len(kw) > 3 or re.search(rf"(?<![\w]){re.escape(kw)}(?![\w])", text) is not None
    )


def excluded_technologies(profile: Profile | None) -> list[str]:
    """Technologie z reguł profilu typu „Azure: brak doświadczenia, nigdy nie wpisywać”."""
    if profile is None:
        return []
    found: list[str] = []
    for rule in profile.rules:
        for m in re.finditer(
            r"([A-Z][\w+#.-]*)\s*:\s*(?:brak doświadczenia|nigdy nie wpisywać)", rule.text
        ):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


@dataclass
class Verdict:
    accepted: bool
    category: str | None
    reason: str


def classify_title(title: str, settings: Settings, profile: Profile | None = None) -> Verdict:
    """Ocena trafności po samym tytule (dane z maila, przed pobraniem strony)."""
    text = normalize(title)
    for tech in excluded_technologies(profile):
        if _contains(text, tech):
            return Verdict(False, None, f"wymaga {tech}, a profil mówi: brak doświadczenia")
    for word in settings.relevance_exclude_keywords:
        if _contains(text, word):
            return Verdict(False, None, f"poziom stanowiska: „{word}”")
    for category, keywords in CATEGORIES.items():
        hit = next((k for k in keywords if _contains(text, k)), None)
        if hit:
            return Verdict(True, category, f"{CATEGORY_LABELS[category]} (słowo „{hit}”)")
    # Oferty w mailach są już przefiltrowane przez wyszukiwania ustawione w portalach,
    # więc brak kategorii nie odrzuca oferty; kategoria służy tylko jako etykieta.
    return Verdict(True, None, "bez kategorii (oferta z wyszukiwania w portalu)")


# --- deduplikacja i zapis --------------------------------------------------------------


# Dopiski o płci w tytułach: „(k/m/n)”, „(m/k/os.)”, „(K/M/X)”, „(k,m)”, „(m/f/d)”.
_GENDER_NOTE = re.compile(r"\((?:\s|[kmfdnx*/,|.]|os)+\)")
_FEMININE_SUFFIX = re.compile(r"/\s*-?\s*(?:ka|ca|a)\b")  # „Administrator/-ka”
# Formy prawne firm, różnie zapisywane na różnych portalach.
_LEGAL_FORM = re.compile(
    r"\b(?:sp\.?\s*z\s*o\.?\s*o\.?|spolka z ograniczona odpowiedzialnoscia|sp\.?\s*k\.?"
    r"|sp\.?\s*j\.?|s\.?\s*a\.?|s\.?\s*c\.?|sp\.?\s*p\.?|ltd\.?|gmbh|inc\.?|llc"
    r"|zrt\.?|s\.?\s*r\.?\s*o\.?|b\.?\s*v\.?)(?=\W|$)"
)


def _key_text(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def dedupe_title(title: str) -> str:
    """Tytuł do porównania ofert między portalami (bez dopisków o płci i interpunkcji)."""
    text = _FEMININE_SUFFIX.sub("", _GENDER_NOTE.sub(" ", normalize(title)))
    return _key_text(text)


def dedupe_company(company: str | None) -> str:
    """Firma do porównania ofert między portalami („Mindbox Sp. z o.o.” → „mindbox”)."""
    return _key_text(_LEGAL_FORM.sub(" ", normalize(company or "")))


def _same_offer_key(title: str, company: str | None) -> tuple[str, str]:
    return dedupe_title(title), dedupe_company(company)


@dataclass
class IngestResult:
    added: list[Offer] = field(default_factory=list)  # nowe, przyjęte przez filtr
    rejected: list[Offer] = field(default_factory=list)  # nowe, odrzucone przez filtr
    duplicates: int = 0  # pominięte: już w bazie albo powtórzone w tym pobraniu

    @property
    def summary(self) -> str:
        return (
            f"Nowe oferty: {len(self.added)}, odrzucone przez filtr: {len(self.rejected)}, "
            f"duplikaty: {self.duplicates}"
        )


def ingest_mail_offers(
    session: Session,
    mail_offers: list[MailOffer],
    settings: Settings,
    profile: Profile | None = None,
    *,
    now: datetime | None = None,
) -> IngestResult:
    """Zapisuje oferty z maili: deduplikacja, filtr trafności, status i kategoria.

    Duplikat to oferta o tym samym kluczu ``(source, external_id)`` albo ta sama
    oferta (tytuł + firma) dodana w ostatnich 30 dniach pod innym numerem lub z innego
    portalu.
    """
    now = now or datetime.now(UTC)
    result = IngestResult()
    seen_keys: set[tuple[str, str]] = set()
    seen_reposts: set[tuple[str, str]] = set()

    recent = session.exec(
        select(Offer.title, Offer.company, Offer.created_at).where(
            Offer.created_at >= now - REPOST_WINDOW
        )
    ).all()
    seen_reposts.update(_same_offer_key(t, c) for t, c, _ in recent if dedupe_company(c))

    for item in mail_offers:
        if item.key in seen_keys:
            result.duplicates += 1
            continue
        seen_keys.add(item.key)
        existing = session.exec(
            select(Offer.id).where(
                Offer.source == item.source, Offer.external_id == item.external_id
            )
        ).first()
        repost_key = _same_offer_key(item.title, item.company)
        if existing is not None or (repost_key[1] and repost_key in seen_reposts):
            result.duplicates += 1
            continue
        if repost_key[1]:
            seen_reposts.add(repost_key)

        verdict = classify_title(item.title, settings, profile)
        offer = Offer(
            source=item.source,
            external_id=item.external_id,
            title=item.title,
            url=item.url,
            company=item.company,
            locations=list(item.locations),
            salary=item.salary,
            mail_message_id=item.mail_message_id,
            mail_date=item.mail_date,
            category=verdict.category,
            filter_reason=verdict.reason,
            status=OfferStatus.NEW if verdict.accepted else OfferStatus.REJECTED,
        )
        session.add(offer)
        (result.added if verdict.accepted else result.rejected).append(offer)

    session.commit()
    for offer in result.added + result.rejected:
        session.refresh(offer)
    return result


def restore_rejected(session: Session, offer: Offer) -> Offer:
    """Przywraca ofertę odrzuconą przez filtr (np. przycisk w panelu)."""
    offer.status = OfferStatus.NEW
    offer.filter_reason = "przywrócona ręcznie"
    session.add(offer)
    session.commit()
    session.refresh(offer)
    return offer
