"""Panel: przycisk „Pobierz oferty”, lista ofert, przywracanie i podgląd CV."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from test_gmail import FakeService, raw_mail

from cv_app import gmail, offer_page
from cv_app.app import create_app
from cv_app.claude_client import FakeClaude, use_runner
from cv_app.jobs import Job, JobBusy, JobRunner
from cv_app.models import Offer, OfferStatus
from cv_app.pipeline import PipelineError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def panel(settings, monkeypatch):
    settings.offer_fetch_delay = 0
    monkeypatch.setattr(gmail, "load_credentials", lambda s, interactive: object())
    monkeypatch.setattr(gmail, "build_service", lambda creds: FakeService({"m1": raw_mail(24)}))
    page = (FIXTURES / "offers" / "pracuj_offer.html").read_text(encoding="utf-8")
    fetched: list[str] = []

    def fake_fetch(url, s):
        fetched.append(url)
        return page

    monkeypatch.setattr(offer_page, "http_fetch", fake_fetch)
    with TestClient(create_app(settings, jobs=JobRunner(background=False))) as c:
        c.fetched = fetched
        yield c


def test_fetch_button_enabled(panel):
    html = panel.get("/").text
    assert 'id="fetch-offers"' in html
    assert "disabled" not in html.split('id="fetch-offers"')[1].split(">")[0]


def test_fetch_offers_job_lists_offers(panel):
    job = panel.post("/api/offers/fetch").json()
    assert job["status"] == "done", job
    assert any("Maile: 1" in line for line in job["log"])

    data = panel.get("/api/offers").json()
    total = job["result"]["added"] + job["result"]["rejected"]
    assert total == 3
    assert len(data["offers"]) == job["result"]["added"] > 0
    assert len(panel.fetched) == job["result"]["added"]
    assert data["offers"][0]["fetched_at"] is not None

    html = panel.get("/").text
    for offer in data["offers"]:
        assert offer["title"] in html
    assert "Podgląd CV" in html
    assert "gotowe" in html  # ostatnie zadanie widać po odświeżeniu

    again = panel.post("/api/offers/fetch").json()
    assert again["result"] == {"added": 0, "rejected": 0}  # deduplikacja


def test_fetch_offers_gmail_error(panel, monkeypatch):
    def no_login(s, interactive):
        raise gmail.GmailError("Brak pliku credentials.json")

    monkeypatch.setattr(gmail, "load_credentials", no_login)
    job = panel.post("/api/offers/fetch").json()
    assert job["status"] == "error"
    assert "credentials.json" in job["error"]
    assert "błąd" in panel.get("/").text


def _add_offer(client, **fields) -> int:
    with Session(client.app.state.engine) as session:
        offer = Offer(source="pracuj", external_id="9", title="Junior DevOps", **fields)
        session.add(offer)
        session.commit()
        return offer.id


def test_restore_rejected_offer(panel):
    offer_id = _add_offer(
        panel, url="https://www.pracuj.pl/praca/x,oferta,9", status=OfferStatus.REJECTED
    )
    html = panel.get("/").text
    assert "Odrzucone przez filtr (1)" in html

    job = panel.post(f"/api/offers/{offer_id}/restore").json()
    assert job["status"] == "done", job
    data = panel.get("/api/offers").json()
    assert [o["id"] for o in data["offers"]] == [offer_id]
    assert data["rejected"] == []
    assert panel.fetched == ["https://www.pracuj.pl/praca/x,oferta,9"]


def test_cv_preview_job_and_pdf(panel, settings, monkeypatch):
    from test_generator import answer

    from cv_app.fit import ColumnFit, FitReport

    shutil.copytree(FIXTURES / "profile_sample", settings.notes_dir)
    offer_id = _add_offer(panel)
    report = FitReport(
        main=ColumnFit(overflow_mm=0, free_mm=10), side=ColumnFit(overflow_mm=0, free_mm=3)
    )
    monkeypatch.setattr("cv_app.fit.measure_fit", lambda html, s: report)

    def fake_pdf(html, out, s):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 test")
        return out

    monkeypatch.setattr("cv_app.render.render_pdf", fake_pdf)

    assert panel.get(f"/offers/{offer_id}/cv-preview.pdf").status_code == 404
    with use_runner(FakeClaude([answer()])):
        job = panel.post(f"/api/offers/{offer_id}/cv-preview").json()
    assert job["status"] == "done", job
    assert job["result"]["pdf_url"] == f"/offers/{offer_id}/cv-preview.pdf"

    r = panel.get(job["result"]["pdf_url"])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert "Otwórz PDF" in panel.get("/").text


def test_cv_preview_unknown_offer(panel):
    assert panel.post("/api/offers/999/cv-preview").status_code == 404


def test_only_one_job_at_a_time(panel):
    runner: JobRunner = panel.app.state.jobs
    runner.current = Job(id=99, kind="fetch", label="Pobieranie ofert z Gmaila")
    r = panel.post("/api/offers/fetch")
    assert r.status_code == 409
    assert "Trwa już" in r.json()["detail"]
    assert panel.get("/api/jobs/current").json()["id"] == 99


def test_job_runner_records_errors():
    runner = JobRunner(background=False)

    def fail(log):
        log("krok 1")
        raise PipelineError("Gmail: brak sieci")

    job = runner.start("fetch", "x", fail)
    assert job.status == "error" and job.error == "Gmail: brak sieci"
    assert job.log == ["krok 1"]
    assert runner.get(job.id) is job

    boom = runner.start("fetch", "y", lambda log: 1 / 0)
    assert boom.status == "error" and "Nieoczekiwany" in boom.error


def test_job_runner_background_thread():
    import threading

    gate = threading.Event()
    runner = JobRunner()
    job = runner.start("fetch", "x", lambda log: gate.wait(5) and {"ok": True})
    with pytest.raises(JobBusy):
        runner.start("fetch", "y", lambda log: None)
    gate.set()
    for _ in range(100):
        if job.status != "running":
            break
        threading.Event().wait(0.01)
    assert job.status == "done" and job.result == {"ok": True}
