"""Adaptery JustJoin.it, No Fluff Jobs i theprotocol.it (zmyślone maile w fixtures)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cv_app.mail import MailMessage
from cv_app.portals import adapter_for, all_senders, offers_from_mail, source_label
from cv_app.portals.common import clean_url, unwrap_tracking

MAIL = Path(__file__).parent / "fixtures" / "mail"


def message(name: str, sender: str) -> MailMessage:
    return MailMessage(
        id=f"msg-{name}",
        sender=sender,
        subject="Nowe oferty",
        date=datetime(2026, 11, 26, 8, 0, tzinfo=UTC),
        html=(MAIL / f"{name}_sample.html").read_text(encoding="utf-8"),
    )


def test_unwrap_nofluff_tracker():
    target = "https://nofluffjobs.com/pl/job/devops-firma-remote?criteria=&utm_source=email"
    enc = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    tracked = f"https://mail-link.nofluffjobs.com/click/abc123/{enc}/def456"
    assert unwrap_tracking(tracked) == target
    assert (
        clean_url(unwrap_tracking(tracked)) == "https://nofluffjobs.com/pl/job/devops-firma-remote"
    )


def test_unwrap_redirect_param_and_plain_link():
    assert (
        unwrap_tracking("https://click.example.com/r?url=https%3A%2F%2Fjustjoin.it%2Fjob-offer%2Fx")
        == "https://justjoin.it/job-offer/x"
    )
    plain = "https://justjoin.it/job-offer/firma-junior-admin-krakow"
    assert unwrap_tracking(plain) == plain


@pytest.mark.parametrize(
    "sender, source",
    [
        ("no-reply@justjoin.it", "justjoin"),
        ("notifications@nofluffjobs.com", "nofluff"),
        ("jobalert@mailing.theprotocol.it", "theprotocol"),
        ("jobalert@wysylka.pracuj.pl", "pracuj"),
    ],
)
def test_adapter_for_senders(sender, source, settings):
    assert adapter_for(sender).source == source
    assert sender in all_senders()
    assert sender in settings.senders  # domyślnie Gmail pyta o wszystkie portale


def test_justjoin_offers():
    offers = offers_from_mail(message("justjoin", "no-reply@justjoin.it"))
    assert len(offers) == 2
    net, admin = offers
    assert net.key == ("justjoin", "firma-testowa-sp-z-o-o--junior-network-engineer-katowice-admin")
    assert net.url == (
        "https://justjoin.it/job-offer/firma-testowa-sp-z-o-o--junior-network-engineer-katowice-admin"
    )
    assert net.title == "Junior Network Engineer"
    assert net.company == "Firma Testowa Sp. z o.o."
    assert net.locations == ["Katowice"]
    assert net.salary == "5000 - 8000 PLN"
    assert net.mail_message_id == "msg-justjoin"
    assert admin.title == "Młodszy Administrator Systemów Linux (k/m/n)"
    assert admin.salary is None  # „Undisclosed salary”


def test_nofluff_offers_from_tracked_links():
    offers = offers_from_mail(message("nofluff", "notifications@nofluffjobs.com"))
    assert [o.external_id for o in offers] == [
        "administrator-ka-systemow-aplikacyjnych-testowa-wroclaw",
        "junior-python-developer-przyklad-remote",
    ]
    admin, python = offers
    assert admin.source == "nofluff"
    assert admin.url == (
        "https://nofluffjobs.com/pl/job/administrator-ka-systemow-aplikacyjnych-testowa-wroclaw"
    )
    assert admin.title == "Administrator/-ka Systemów Aplikacyjnych"
    assert admin.company == "Testowa Sp. z o.o."
    assert admin.locations == ["Wrocław"]
    assert admin.salary == "11 340 - 13 440 PLN"
    assert python.company == "Przykład"
    assert python.locations == ["Zdalnie"]
    assert python.salary is None


def test_theprotocol_offers():
    offers = offers_from_mail(message("theprotocol", "jobalert@mailing.theprotocol.it"))
    assert [o.external_id for o in offers] == [
        "01000000-aaaa-bbbb-cccc-000000000001",
        "01000000-aaaa-bbbb-cccc-000000000002",
        "01000000-aaaa-bbbb-cccc-000000000003",
    ]
    py, devops, manager = offers
    assert py.url == (
        "https://theprotocol.it/szczegoly/praca/junior-python-backend-developer-warszawa,"
        "oferta,01000000-aaaa-bbbb-cccc-000000000001"
    )
    assert py.title == "Junior Python Backend Developer"
    assert py.salary == "maks. 8 500 zł / mies."
    assert py.company == "Fundacja Testowa"
    assert py.locations == ["Warszawa, Żoliborz"]
    assert devops.salary is None
    assert devops.company == "Chmura - Test Sp. z o.o."
    assert devops.locations == ["Kraków"]
    assert manager.title == "Engineering Manager"  # polecana oferta też trafia do filtra


def test_empty_html_gives_no_offers():
    msg = message("justjoin", "no-reply@justjoin.it").model_copy(update={"html": None})
    assert offers_from_mail(msg) == []


def test_source_labels():
    assert source_label("nofluff") == "No Fluff Jobs"
    assert source_label("nieznany") == "nieznany"
