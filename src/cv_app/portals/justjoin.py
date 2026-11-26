"""Adapter JustJoin.it: mail „New jobs for you” (no-reply@justjoin.it).

Mail jest tylko w HTML. Każda oferta to jeden link (karta) do
``https://justjoin.it/job-offer/<slug>?utm_...``; w środku elementy z klasami
``company-name``, ``company-city``, ``offer-title``, ``salary`` („5000 - 8000 PLN”
albo „Undisclosed salary”) i ``offer-details`` (tryb pracy, umowa). Numerem
oferty jest ``slug``. Link może być owinięty trackerem kliknięć, więc najpierw go
odpakowujemy.
"""

from __future__ import annotations

import re
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer
from cv_app.portals.common import clean_url, text_of, unwrap_tracking

OFFER_URL = re.compile(r"^https?://(?:www\.)?justjoin\.it/job-offer/([\w-]+)", re.I)


def _first(card: Tag, cls: str) -> str | None:
    node = card.select_one(f".{cls}")
    text = text_of(node) if node else ""
    return text or None


def _salary(card: Tag) -> str | None:
    """„5000 - 8000 PLN”; ``None`` dla „Undisclosed salary”."""
    text = _first(card, "salary")
    return text if text and re.search(r"\d", text) else None


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").strip().capitalize()


class JustJoinAdapter:
    source: ClassVar[str] = "justjoin"
    senders: ClassVar[frozenset[str]] = frozenset({"no-reply@justjoin.it"})

    def parse(self, message: MailMessage) -> list[MailOffer]:
        if not message.html:
            return []
        return self.parse_html(message.html, message)

    def parse_html(self, html: str, message: MailMessage | None = None) -> list[MailOffer]:
        soup = BeautifulSoup(html, "html.parser")
        offers: dict[str, MailOffer] = {}
        for a in soup.find_all("a", href=True):
            url = clean_url(unwrap_tracking(a["href"]))
            m = OFFER_URL.match(url)
            if not m or m.group(1) in offers:
                continue
            slug = m.group(1)
            city = _first(a, "company-city")
            offers[slug] = MailOffer(
                source=self.source,
                external_id=slug,
                title=_first(a, "offer-title") or _title_from_slug(slug),
                url=url,
                company=_first(a, "company-name"),
                locations=[city] if city else [],
                salary=_salary(a),
                mail_message_id=message.id if message else None,
                mail_date=message.date if message else None,
            )
        return list(offers.values())
