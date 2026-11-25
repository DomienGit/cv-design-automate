from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from cv_app.db import init_db, make_engine
from cv_app.models import CV, CVStatus, Offer, OfferStatus


@pytest.fixture
def session(settings):
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_database_file_created_in_data_dir(settings):
    engine = make_engine(settings)
    init_db(engine)
    assert settings.database_path.is_file()
    assert settings.database_path.parent == settings.data_dir


def test_offer_and_cv_roundtrip(session):
    offer = Offer(
        source="pracuj",
        external_id="1005236889",
        title="Helpdesk II linii",
        company="Kamsoft",
        locations=["Katowice"],
        requirements=["Linux", "sieci"],
        responsibilities=["wsparcie użytkowników"],
    )
    session.add(offer)
    session.commit()
    cv = CV(offer_id=offer.id, language="pl", blocks={"summary": "..."})
    session.add(cv)
    session.commit()

    loaded = session.exec(select(Offer)).one()
    assert loaded.status is OfferStatus.NEW
    assert loaded.locations == ["Katowice"]
    assert loaded.requirements == ["Linux", "sieci"]
    assert [c.blocks for c in loaded.cvs] == [{"summary": "..."}]
    assert loaded.cvs[0].status is CVStatus.QUEUED


def test_offer_is_unique_per_source(session):
    session.add(Offer(source="pracuj", external_id="1", title="A"))
    session.commit()
    session.add(Offer(source="pracuj", external_id="1", title="A (duplikat)"))
    with pytest.raises(IntegrityError):
        session.commit()
