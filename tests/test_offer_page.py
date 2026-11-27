from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from cv_app.claude_client import ClaudeError, FakeClaude, use_runner
from cv_app.db import init_db, make_engine
from cv_app.models import Offer
from cv_app.offer_page import (
    FetchError,
    OfferPageService,
    PageCache,
    RateLimiter,
    detect_language,
    extract_from_html,
    visible_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "offers"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def session(settings):
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def make_offer(session: Session, external_id: str = "1", **kw) -> Offer:
    offer = Offer(
        source="pracuj",
        external_id=external_id,
        title=kw.pop("title", "Administrator Linux"),
        url=kw.pop("url", f"https://www.pracuj.pl/praca/x,oferta,{external_id}"),
        company=kw.pop("company", "Firma z maila"),
        locations=kw.pop("locations", ["Katowice"]),
        **kw,
    )
    session.add(offer)
    session.commit()
    session.refresh(offer)
    return offer


class FakeFetcher:
    def __init__(self, pages: dict[str, str | Exception]):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url, settings):
        self.calls.append(url)
        item = self.pages[url]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_extract_pracuj_sections():
    d = extract_from_html(page("pracuj_offer.html"))
    assert d.title == "Młodszy Administrator Linux"
    assert d.company == "Testowa Firma Sp. z o.o."
    assert d.responsibilities == ["Administracja serwerami Linux", "Utrzymanie kontenerów Docker"]
    assert d.requirements == [
        "Znajomość systemu Linux i powłoki Bash",
        "Komunikatywność",
        "Znajomość Ansible (mile widziane)",
    ]
    assert d.technologies == ["Linux", "Bash", "Ansible (mile widziane)"]
    assert d.language == "pl"
    assert "Karta sportowa" not in d.requirements


def test_extract_json_ld():
    d = extract_from_html(page("jsonld_offer.html"))
    assert d.company == "Example Cloud Ltd"
    assert d.locations == ["Katowice", "Gliwice"]
    assert d.requirements == ["Experience with Docker and Terraform", "Good knowledge of Linux"]
    assert d.responsibilities[0] == "Maintain CI/CD pipelines"
    assert d.technologies == ["Docker", "Terraform", "AWS"]
    assert d.language == "en"


def test_extract_by_headings():
    d = extract_from_html(page("headings_offer.html"))
    assert d.title == "Python Developer"
    assert d.responsibilities == ["Develop REST APIs with FastAPI", "Write tests for the backend"]
    assert d.requirements[0] == "Knowledge of Python and SQL"
    assert d.language == "en"


def test_empty_page_has_no_content():
    d = extract_from_html("<html><body><p>Ładowanie...</p></body></html>")
    assert not d.has_content


def test_detect_language():
    assert detect_language("Znajomość systemu Linux oraz doświadczenie w pracy z Dockerem") == "pl"
    assert detect_language("Experience with Linux and knowledge of the cloud") == "en"


def test_visible_text_skips_scripts_and_nav():
    text = visible_text(page("pracuj_offer.html"))
    assert "tracking" not in text and "menu" not in text
    assert "Nasze wymagania" in text


def test_rate_limiter_waits_between_calls():
    fake = FakeClock()
    limiter = RateLimiter(2.0, clock=fake.clock, sleep=fake.sleep)
    limiter.wait()
    fake.now += 0.5
    limiter.wait()
    limiter.wait()
    assert fake.sleeps == [1.5, 2.0]


def test_cache_expiry(settings):
    cache = PageCache(settings)
    cache.put("pracuj", "1", "<html>x</html>")
    assert cache.get("pracuj", "1") == "<html>x</html>"
    old = time.time() - (settings.offer_cache_days + 1) * 86400
    os.utime(cache.path("pracuj", "1"), (old, old))
    assert cache.get("pracuj", "1") is None
    assert cache.path("pracuj", "../x").parent == cache.root / "pracuj"


def service(settings, pages, **kw):
    fake = FakeClock()
    fetcher = FakeFetcher(pages)
    svc = OfferPageService(
        settings,
        fetcher=fetcher,
        limiter=RateLimiter(settings.offer_fetch_delay, clock=fake.clock, sleep=fake.sleep),
        **kw,
    )
    return svc, fetcher, fake


def test_enrich_all_uses_cache_and_rate_limit(session, settings):
    a = make_offer(session, "1")
    b = make_offer(session, "2", company=None, locations=[])
    pages = {a.url: page("pracuj_offer.html"), b.url: page("jsonld_offer.html")}
    svc, fetcher, fake = service(settings, pages)

    result = svc.enrich_all(session, [a, b])
    assert result.fetched == 2 and not result.errors
    assert fake.sleeps == [2.0]  # druga strona dopiero po 2 s
    assert a.company == "Firma z maila"  # dane z maila mają pierwszeństwo
    assert a.requirements[0].startswith("Znajomość systemu Linux")
    assert a.technologies == ["Linux", "Bash", "Ansible (mile widziane)"]
    assert a.language == "pl" and not a.incomplete and a.fetched_at is not None
    assert b.company == "Example Cloud Ltd" and b.locations == ["Katowice", "Gliwice"]
    assert b.language == "en"

    svc2, fetcher2, _ = service(settings, pages)
    svc2.enrich_all(session, [a, b])
    assert fetcher2.calls == []  # wszystko z cache


def test_enrich_failure_keeps_mail_data(session, settings):
    offer = make_offer(session, "3")
    svc, _, _ = service(settings, {offer.url: FetchError("kod 404")})
    result = svc.enrich_all(session, [offer])
    assert result.incomplete == [offer]
    assert "404" in result.errors[offer.id]
    session.refresh(offer)
    assert offer.incomplete and offer.title == "Administrator Linux"
    assert offer.locations == ["Katowice"] and offer.fetched_at is None


def test_enrich_falls_back_to_ai(session, settings):
    offer = make_offer(session, "4")
    html = "<html><body><div>Wymagamy: Linux, Bash. Zajmiesz się serwerami.</div></body></html>"
    svc, _, _ = service(settings, {offer.url: html})
    fake = FakeClaude(
        [
            {
                "title": "Administrator",
                "company": None,
                "language": "pl",
                "locations": [],
                "requirements": ["Linux", "Bash"],
                "responsibilities": ["Utrzymanie serwerów"],
                "technologies": ["Linux", "Bash"],
            }
        ]
    )
    with use_runner(fake):
        svc.enrich(offer)
    assert offer.requirements == ["Linux", "Bash"] and not offer.incomplete
    prompt = fake.calls[0][0]
    assert "Wymagamy: Linux, Bash" in prompt and "<div>" not in prompt


def test_enrich_ai_error_marks_incomplete(session, settings):
    offer = make_offer(session, "5")
    settings = settings.model_copy(update={"claude_retries": 0})
    svc, _, _ = service(settings, {offer.url: "<html><body>pusto</body></html>"})
    with use_runner(FakeClaude([ClaudeError("x")])):
        result = svc.enrich_all(session, [offer])
    assert result.incomplete == [offer] and offer.incomplete


def test_enrich_without_ai_marks_incomplete(session, settings):
    offer = make_offer(session, "6")
    svc, _, _ = service(settings, {offer.url: "<html><body>pusto</body></html>"}, use_ai=False)
    svc.enrich(offer)
    assert offer.incomplete and offer.fetched_at is not None


def test_extracts_sections_from_json_ld_description():
    from cv_app.offer_page import extract_from_html

    description = (
        "<h3>Wymagania obowiązkowe</h3><ul><li>Linux</li><li>Docker</li></ul>"
        "<h3>Twoje zadania</h3><ul><li>Utrzymanie CI/CD</li></ul>"
    )
    ld = json.dumps({"@type": "JobPosting", "title": "Junior DevOps", "description": description})
    html = (
        f'<html><head><script type="application/ld+json">{ld}</script></head>'
        "<body><h1>Junior DevOps</h1></body></html>"
    )
    details = extract_from_html(html)
    assert details.requirements == ["Linux", "Docker"]
    assert details.responsibilities == ["Utrzymanie CI/CD"]
