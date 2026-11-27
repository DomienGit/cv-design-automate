from __future__ import annotations

import os
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from cv_app.mail import MailMessage, parse_raw_message
from cv_app.portals import adapter_for, offers_from_mail
from cv_app.portals.pracuj import PracujAdapter, clean_url

SAMPLE = Path(__file__).parent / "fixtures" / "mail" / "pracuj_sample.html"


def sample_message(html: str | None = None, sender: str = "jobalert@wysylka.pracuj.pl"):
    return MailMessage(
        id="msg-1",
        sender=sender,
        subject="Oferty na które czekasz",
        date=datetime(2026, 11, 24, 1, 48, tzinfo=UTC),
        html=html if html is not None else SAMPLE.read_text(encoding="utf-8"),
    )


def test_clean_url_drops_sendid_and_utm():
    url = "https://www.pracuj.pl/praca/x-katowice,oferta,123?sendid=a&utm_source=ja#top"
    assert clean_url(url) == "https://www.pracuj.pl/praca/x-katowice,oferta,123"


def test_parses_offers_from_sample():
    offers = offers_from_mail(sample_message())
    assert [o.external_id for o in offers] == ["1000000001", "1000000002", "1000000003"]

    admin, helpdesk, python = offers
    assert admin.key == ("pracuj", "1000000001")
    assert admin.title == "Administrator Linux (K/M/X)"
    assert admin.company == "Firma Testowa sp. z o.o."
    assert admin.salary == "8 000-11 000 zł / mies. (zal. od umowy)"
    assert admin.locations == ["Katowice", "Gliwice"]
    assert admin.url == "https://www.pracuj.pl/praca/administrator-linux-katowice,oferta,1000000001"
    assert admin.mail_message_id == "msg-1"
    assert admin.mail_date.year == 2026

    assert helpdesk.title == "Specjalista ds. Helpdesk"  # bez ikonki „!”
    assert helpdesk.salary is None
    assert helpdesk.locations == ["Zabrze"]

    # Sam link z logo: tytuł z adresu, reszta pusta.
    assert python.title == "Python developer katowice"
    assert python.company is None


def test_other_sender_and_empty_mail():
    assert offers_from_mail(sample_message(sender="ktos@example.com")) == []
    assert offers_from_mail(sample_message(html="")) == []
    assert adapter_for("JobAlert@Wysylka.Pracuj.pl") is not None


def test_parse_raw_message_html_only():
    msg = EmailMessage()
    msg["From"] = '"Wyszukiwane w Pracuj.pl" <jobalert@wysylka.pracuj.pl>'
    msg["Subject"] = "Oferty"
    msg["Date"] = "Tue, 24 Nov 2026 02:48:55 +0100"
    msg["Message-ID"] = "<abc@pracuj.pl>"
    msg.set_content(SAMPLE.read_text(encoding="utf-8"), subtype="html")
    parsed = parse_raw_message(msg.as_bytes())
    assert parsed.sender == "jobalert@wysylka.pracuj.pl"
    assert parsed.id == "abc@pracuj.pl"
    assert parsed.text is None
    assert len(PracujAdapter().parse(parsed)) == 3


def test_real_mail_from_24_11():
    """Prawdziwy mail (.eml) z 24.11.2026; ścieżka w PRACUJ_SAMPLE_EML, w repo go nie ma."""
    path = os.environ.get("PRACUJ_SAMPLE_EML")
    if not path or not Path(path).is_file():
        pytest.skip("brak PRACUJ_SAMPLE_EML z prawdziwym mailem")
    offers = offers_from_mail(parse_raw_message(Path(path).read_bytes()))
    assert len(offers) == 13
    assert len({o.external_id for o in offers}) == 13
    assert all(o.title and o.company and o.locations for o in offers)
    assert all("sendid" not in o.url for o in offers)
