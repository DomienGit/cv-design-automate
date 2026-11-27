"""Adapter No Fluff Jobs: mail „Nowe oferty pracy” (notifications@nofluffjobs.com).

Wszystkie linki idą przez tracker
``https://mail-link.nofluffjobs.com/click/<id>/<base64 adresu>/<id>``; docelowy adres
oferty ``https://nofluffjobs.com/pl/job/<slug>?criteria=...&utm_...`` jest zakodowany
base64 (URL-safe) w ścieżce. Numerem oferty jest ``slug``. Karta oferty to jeden link:
tytuł w ``h1.posting-content__title``, widełki w ``span.salary-label``, potem dwa
``<span>`` bez klasy: firma i miejsce („in Wrocław”).
"""

from __future__ import annotations

import re
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer
from cv_app.portals.common import looks_like_salary, text_of, unwrap_tracking

OFFER_URL = re.compile(
    r"^https?://(?:www\.)?nofluffjobs\.com/(?:(?:pl|en|cz|sk|hu|ua)/)?job/([\w-]+)", re.I
)
_PLACE_PREFIX = re.compile(r"^(?:in|w|we)\s+", re.I)


def _details(card: Tag) -> tuple[str | None, str | None, str | None, list[str]]:
    title_node = card.select_one(".posting-content__title") or card.find(["h1", "h2", "h3"])
    title = text_of(title_node) if title_node else None
    salary_node = card.select_one(".salary-label")
    salary = text_of(salary_node) if salary_node else None
    if salary and not looks_like_salary(salary):
        salary = None

    rest: list[str] = []
    for span in card.find_all("span"):
        if span.get("class") or span.find("span"):
            continue
        text = text_of(span)
        if text and text not in (title, salary) and text not in rest:
            rest.append(text)
    company = rest[0] if rest else None
    places = [_PLACE_PREFIX.sub("", t) for t in rest[1:2]]
    return title, salary, company, [p for p in places if p]


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").strip().capitalize()


class NoFluffAdapter:
    source: ClassVar[str] = "nofluff"
    senders: ClassVar[frozenset[str]] = frozenset({"notifications@nofluffjobs.com"})

    def parse(self, message: MailMessage) -> list[MailOffer]:
        if not message.html:
            return []
        return self.parse_html(message.html, message)

    def parse_html(self, html: str, message: MailMessage | None = None) -> list[MailOffer]:
        soup = BeautifulSoup(html, "html.parser")
        offers: dict[str, MailOffer] = {}
        for a in soup.find_all("a", href=True):
            target = unwrap_tracking(a["href"])
            m = OFFER_URL.match(target)
            if not m:
                continue
            slug = m.group(1)
            title, salary, company, places = _details(a)
            if slug in offers and not title:
                continue  # np. logo albo przycisk do tej samej oferty
            if slug in offers and offers[slug].company:
                continue  # karta już odczytana
            offers[slug] = MailOffer(
                source=self.source,
                external_id=slug,
                title=title or _title_from_slug(slug),
                url=f"https://nofluffjobs.com/pl/job/{slug}",
                company=company,
                locations=places,
                salary=salary,
                mail_message_id=message.id if message else None,
                mail_date=message.date if message else None,
            )
        return list(offers.values())
