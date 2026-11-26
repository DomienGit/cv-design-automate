from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select

from cv_app.db import init_db, make_engine
from cv_app.models import Offer, OfferStatus
from cv_app.portals.base import MailOffer
from cv_app.profile import load_profile
from cv_app.relevance import (
    classify_title,
    excluded_technologies,
    ingest_mail_offers,
    normalize,
    restore_rejected,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


@pytest.fixture
def profile(settings):
    shutil.copytree(FIXTURES, settings.notes_dir)
    return load_profile(settings)


@pytest.fixture
def session(settings):
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def mail_offer(external_id: str, title: str, company: str | None = "Firma") -> MailOffer:
    return MailOffer(
        source="pracuj",
        external_id=external_id,
        title=title,
        url=f"https://www.pracuj.pl/praca/x,oferta,{external_id}",
        company=company,
        locations=["Katowice"],
        mail_date=datetime(2026, 11, 24, tzinfo=UTC),
    )


def test_normalize_strips_polish_letters():
    assert normalize("  Administrator  Systemów ŁĄCZNOŚCI ") == "administrator systemow lacznosci"


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Administrator Linux (K/M/X)", "admin"),
        ("Młodszy Administrator Systemów IT", "admin"),
        ("Junior DevOps Engineer", "devops"),
        ("Inżynier ds. rozwiązań chmurowych", "devops"),
        ("Specjalista ds. wsparcia IT / Helpdesk", "helpdesk"),
        ("Service Desk Analyst with English", "helpdesk"),
        ("Python Developer", "python"),
        ("Junior Backend Developer (FastAPI)", "python"),
        ("AI Engineer (LLM)", "ai"),
        ("Python Developer – AI / ML", "ai"),
        ("Technik informatyk", "helpdesk"),
        ("Młodszy specjalista IT", "admin"),
    ],
)
def test_classify_accepts_categories(settings, title, category):
    verdict = classify_title(title, settings)
    assert verdict.accepted, verdict
    assert verdict.category == category


@pytest.mark.parametrize(
    ("title", "reason_part"),
    [
        ("Senior DevOps Engineer", "senior"),
        ("Sr. Python Developer", "sr"),
        ("Team Leader Service Desk", "lead"),
        ("IT Manager", "manager"),
        ("Administrator nieruchomości", "nie pasuje"),
        ("Specjalista ds. administracji biura", "nie pasuje"),
        ("Magazynier", "nie pasuje"),
        ("Specjalista ds. automatyzacji procesów produkcyjnych", "nie pasuje"),
        ("Specjalista ds. obsługi klienta", "nie pasuje"),
    ],
)
def test_classify_rejects(settings, title, reason_part):
    verdict = classify_title(title, settings)
    assert not verdict.accepted
    assert reason_part in verdict.reason


def test_profile_rule_excludes_azure(settings, profile):
    assert excluded_technologies(profile) == ["Azure"]
    verdict = classify_title("Junior Azure Cloud Engineer", settings, profile)
    assert not verdict.accepted and "Azure" in verdict.reason
    assert classify_title("Junior Azure Cloud Engineer", settings).accepted  # bez profilu


def test_exclude_keywords_from_settings(settings):
    custom = settings.model_copy(update={"relevance_exclude_keywords": ["junior"]})
    assert not classify_title("Junior DevOps", custom).accepted
    assert classify_title("Senior DevOps", custom).accepted


def test_ingest_dedup_and_filter(session, settings, profile):
    offers = [
        mail_offer("1", "Administrator Linux"),
        mail_offer("1", "Administrator Linux"),  # ten sam numer w tym samym pobraniu
        mail_offer("2", "Senior DevOps Engineer"),
        mail_offer("3", "Python Developer", company="Inna"),
    ]
    result = ingest_mail_offers(session, offers, settings, profile)
    assert [o.external_id for o in result.added] == ["1", "3"]
    assert [o.external_id for o in result.rejected] == ["2"]
    assert result.duplicates == 1
    assert "Nowe oferty: 2" in result.summary

    rows = {o.external_id: o for o in session.exec(select(Offer)).all()}
    assert rows["1"].category == "admin" and rows["1"].status == OfferStatus.NEW
    assert rows["1"].locations == ["Katowice"] and rows["1"].filter_reason
    assert rows["2"].status == OfferStatus.REJECTED and "senior" in rows["2"].filter_reason

    # Drugie pobranie: te same numery i ta sama oferta pod nowym numerem → duplikaty.
    again = ingest_mail_offers(
        session,
        [
            mail_offer("1", "Administrator Linux"),
            mail_offer("99", "Python  developer", company="INNA"),
            mail_offer("4", "Junior DevOps"),
        ],
        settings,
        profile,
    )
    assert again.duplicates == 2
    assert [o.external_id for o in again.added] == ["4"]
    assert len(session.exec(select(Offer)).all()) == 4


def test_restore_rejected(session, settings):
    result = ingest_mail_offers(session, [mail_offer("5", "Magazynier")], settings)
    offer = restore_rejected(session, result.rejected[0])
    assert offer.status == OfferStatus.NEW


def test_dedupe_keys_ignore_gender_notes_and_legal_forms():
    from cv_app.relevance import dedupe_company, dedupe_title

    assert dedupe_title("Administrator/-ka Systemów Linux (k/m/n)") == dedupe_title(
        "Administrator systemów Linux (K/M/X)"
    )
    assert dedupe_title("Młodszy Administrator Aplikacji (m/k/os.)") == (
        "mlodszy administrator aplikacji"
    )
    assert dedupe_company("Mindbox Sp. z o.o.") == dedupe_company("MINDBOX sp. z o. o.")
    assert dedupe_company("Asseco Poland S.A.") == "asseco poland"


def test_ingest_dedup_across_portals(session, settings):
    pracuj = mail_offer("1", "Administrator Linux (K/M/X)", "Testowa sp. z o.o.")
    justjoin = MailOffer(
        source="justjoin",
        external_id="testowa-administrator-linux-k-m-n--katowice-admin",
        title="Administrator Linux (k/m/n)",
        url="https://justjoin.it/job-offer/testowa-administrator-linux-k-m-n--katowice-admin",
        company="Testowa Sp. z o.o.",
    )
    other = justjoin.model_copy(update={"external_id": "inna", "company": "Inna firma"})
    first = ingest_mail_offers(session, [pracuj], settings)
    assert len(first.added) == 1
    second = ingest_mail_offers(session, [justjoin, other], settings)
    assert second.duplicates == 1
    assert [o.company for o in second.added] == ["Inna firma"]
    assert session.exec(select(Offer).where(Offer.source == "justjoin")).one().company == (
        "Inna firma"
    )
