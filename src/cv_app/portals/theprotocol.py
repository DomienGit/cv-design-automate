"""Adapter theprotocol.it: mail „Nowe oferty pracy” (jobalert@mailing.theprotocol.it).

Mail jest tylko w HTML. Oferta to kilka linków do tej samej strony
``https://theprotocol.it/szczegoly/praca/<slug>,oferta,<uuid>?utm_...``: logo, tytuł,
opcjonalnie wynagrodzenie („max 70 zł / hr.”) i „Firma - Miasto, dzielnica”. Linki
nie mają klas, więc rozpoznajemy je po kolejności i treści. Numerem oferty jest ``uuid``.

Pod listą z obserwowanego wyszukiwania mail dokłada sekcję „Inne oferty wybrane dla
Ciebie”; jej linki mają parametr ``sug``. Te oferty pomijamy: bierzemy tylko nowe
oferty z wyszukiwań ustawionych w portalu.
"""

from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer
from cv_app.portals.common import clean_url, looks_like_salary, text_of, unwrap_tracking

OFFER_URL = re.compile(
    r"^https?://(?:www\.)?theprotocol\.it/szczegoly/praca/[^?#]*?,oferta,([0-9a-f-]{20,})",
    re.I,
)


def _title_from_url(url: str) -> str:
    slug = url.rsplit("/", 1)[-1].split(",oferta,")[0]
    return slug.replace("-", " ").strip().capitalize()


def _company_and_place(text: str) -> tuple[str, list[str]]:
    """„ACAISOFT POLAND Sp. z o.o. - Warszawa, Mokotów” → firma, ["Warszawa, Mokotów"]."""
    if " - " in text:
        company, place = text.rsplit(" - ", 1)
        return company.strip(), [place.strip()] if place.strip() else []
    return text, []


class TheProtocolAdapter:
    source: ClassVar[str] = "theprotocol"
    senders: ClassVar[frozenset[str]] = frozenset({"jobalert@mailing.theprotocol.it"})

    def parse(self, message: MailMessage) -> list[MailOffer]:
        if not message.html:
            return []
        return self.parse_html(message.html, message)

    def parse_html(self, html: str, message: MailMessage | None = None) -> list[MailOffer]:
        soup = BeautifulSoup(html, "html.parser")
        links: dict[str, list[tuple[str, Tag]]] = {}
        for a in soup.find_all("a", href=True):
            target = unwrap_tracking(a["href"])
            if "sug" in parse_qs(urlsplit(target).query):
                continue  # „Inne oferty wybrane dla Ciebie”, nie z wyszukiwania
            url = clean_url(target)
            m = OFFER_URL.match(url)
            if m:
                links.setdefault(m.group(1).lower(), []).append((url, a))

        offers = []
        for offer_id, items in links.items():
            url = items[0][0]
            title = salary = company = None
            locations: list[str] = []
            for _, a in items:
                text = text_of(a)
                if not text:
                    continue  # logo
                if title is None:
                    title = text
                elif salary is None and looks_like_salary(text):
                    salary = text
                elif company is None:
                    company, locations = _company_and_place(text)
            offers.append(
                MailOffer(
                    source=self.source,
                    external_id=offer_id,
                    title=title or _title_from_url(url),
                    url=url,
                    company=company,
                    locations=locations,
                    salary=salary,
                    mail_message_id=message.id if message else None,
                    mail_date=message.date if message else None,
                )
            )
        return offers
