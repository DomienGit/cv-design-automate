"""Adapter Pracuj.pl: mail „Oferty na które czekasz” (jobalert@wysylka.pracuj.pl).

Mail jest tylko w HTML i zawiera kilka ofert. Każda oferta to blok z kilkoma linkami
do tej samej strony ``.../praca/<slug>,oferta,<numer>?sendid=...&utm_...``: logo,
tytuł (``a.anchor``), opcjonalnie wynagrodzenie (``a.salary_color``) i pracodawca
(``a.employer_color``), a obok miasto (``span.workplace``). Numer oferty bierzemy
z linku, parametry (``sendid``, ``utm_*``) odcinamy.
"""

from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from cv_app.mail import MailMessage
from cv_app.portals.base import MailOffer

OFFER_URL = re.compile(r"pracuj\.pl/praca/[^\"'\s]*?,oferta,(\d+)", re.I)


def clean_url(url: str) -> str:
    """Adres oferty bez parametrów i fragmentu (m.in. bez ``sendid``)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _classes(node: Tag) -> set[str]:
    return set(node.get("class") or [])


def _offer_container(anchor: Tag, offer_id: str) -> Tag:
    """Najszerszy przodek linku, który zawiera linki tylko do tej jednej oferty."""
    node = anchor
    while node.parent is not None and node.parent.name not in ("[document]", "body"):
        ids = {
            m.group(1)
            for a in node.parent.find_all("a", href=True)
            if (m := OFFER_URL.search(a["href"]))
        }
        if ids != {offer_id}:
            break
        node = node.parent
    return node


def _title_from_slug(url: str) -> str:
    slug = urlsplit(url).path.rsplit("/", 1)[-1].split(",oferta,")[0]
    return slug.replace("-", " ").strip().capitalize()


class PracujAdapter:
    source: ClassVar[str] = "pracuj"
    senders: ClassVar[frozenset[str]] = frozenset({"jobalert@wysylka.pracuj.pl"})

    def parse(self, message: MailMessage) -> list[MailOffer]:
        if not message.html:
            return []
        return self.parse_html(message.html, message)

    def parse_html(self, html: str, message: MailMessage | None = None) -> list[MailOffer]:
        soup = BeautifulSoup(html, "html.parser")
        anchors: dict[str, list[Tag]] = {}
        for a in soup.find_all("a", href=True):
            m = OFFER_URL.search(a["href"])
            if m:
                anchors.setdefault(m.group(1), []).append(a)

        offers = []
        for offer_id, links in anchors.items():
            url = clean_url(links[0]["href"])
            container = _offer_container(links[0], offer_id)
            title = company = salary = None
            for a in links:
                classes = _classes(a)
                for icon in a.select(".exclamation_mark_icon"):
                    icon.decompose()
                text = _text(a)
                if not text:
                    continue  # logo
                if "employer_color" in classes:
                    company = company or text
                elif "salary_color" in classes:
                    salary = salary or text
                elif title is None:
                    title = text
            workplace = container.select_one(".workplace")
            locations = (
                [c.strip() for c in _text(workplace).split(",") if c.strip()] if workplace else []
            )
            offers.append(
                MailOffer(
                    source=self.source,
                    external_id=offer_id,
                    title=title or _title_from_slug(url),
                    url=url,
                    company=company,
                    locations=locations,
                    salary=salary,
                    mail_message_id=message.id if message else None,
                    mail_date=message.date if message else None,
                )
            )
        return offers
