"""Biblioteka gotowych CV: odczyt, wynik dopasowania, decyzja AI i dobór CV dla oferty."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlmodel import Session
from test_generator import answer
from test_validator import blocks

from cv_app.claude_client import ClaudeError, FakeClaude, use_runner
from cv_app.config import Settings
from cv_app.cv_library import (
    anonymize,
    decide_by_score,
    folder_category,
    guess_language,
    offer_terms,
    rank,
    scan_library,
    strip_clause,
)
from cv_app.cv_match import decide, script_decision
from cv_app.db import init_db, make_engine
from cv_app.fit import ColumnFit, FitReport
from cv_app.jobs import JobRunner
from cv_app.models import Offer
from cv_app.pipeline import (
    PipelineError,
    blocks_path,
    match_cv,
    preview_path,
    read_match_report,
    read_preview_report,
)
from cv_app.profile import load_profile
from cv_app.validator import known_technologies, validate_text

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


def make_pdf(path: Path, lines: list[str]) -> Path:
    """Minimalny PDF z tekstem (Helvetica), czytelny dla pypdf."""
    text = " ".join(f"({line.replace('(', '').replace(')', '')}) Tj 0 -14 Td" for line in lines)
    stream = f"BT /F1 11 Tf 40 800 Td {text} ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def profile(settings: Settings):
    shutil.copytree(FIXTURES, settings.notes_dir)
    return load_profile(settings)


@pytest.fixture
def library(settings: Settings) -> Path:
    settings.cv_library_dir = settings.notes_dir.parent / "biblioteka"
    root = settings.cv_library_dir
    # DevOps: źródło JSON + PDF o tej samej nazwie (jedno CV).
    devops = root / "DevOps"
    devops.mkdir(parents=True)
    (devops / "cv-devops.json").write_text(blocks().model_dump_json(), encoding="utf-8")
    make_pdf(devops / "cv-devops.pdf", ["stary PDF"])
    # Helpdesk: sam PDF z danymi kontaktowymi i klauzulą RODO.
    make_pdf(
        root / "Helpdesk" / "cv-helpdesk.pdf",
        [
            "Jan Testowy  +48 500 000 000  jan@example.com",
            "Technik informatyk: Windows, Linux, Bash",
            "Wyrazam zgode na przetwarzanie danych przez pracodawce",
        ],
    )
    (root / "Helpdesk" / ".ukryty.md").write_text("Docker", encoding="utf-8")
    (root / "notatki.docx").write_bytes(b"x")
    return root


def devops_offer(**kw) -> Offer:
    data = {
        "source": "pracuj",
        "external_id": "1",
        "title": "Junior DevOps Engineer",
        "requirements": ["Docker i AWS", "Terraform", "Znajomość Azure mile widziana"],
        "technologies": ["Docker", "AWS", "Terraform", "Azure"],
        "category": "devops",
        "language": "pl",
    }
    data.update(kw)
    return Offer(**data)


# --- biblioteka --------------------------------------------------------------------------


def test_scan_library_pairs_files_and_reads_pdf(library):
    cvs, warnings = scan_library(library)
    by_key = {cv.key: cv for cv in cvs}
    assert set(by_key) == {"DevOps/cv-devops", "Helpdesk/cv-helpdesk"}
    devops = by_key["DevOps/cv-devops"]
    assert devops.blocks is not None and devops.pdf is not None
    assert devops.category == "devops" and "Docker" in devops.text
    assert "stary PDF" not in devops.text  # źródło wygrywa nad PDF
    helpdesk = by_key["Helpdesk/cv-helpdesk"]
    assert helpdesk.blocks is None and helpdesk.category == "helpdesk"
    assert "Windows, Linux" in helpdesk.text
    assert "pracodawce" not in helpdesk.text  # bez klauzuli RODO
    assert warnings == []


def test_scan_library_missing_folder_and_bad_json(tmp_path):
    assert scan_library(tmp_path / "brak") == ([], [])
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "zly.json").write_text("{}", encoding="utf-8")
    cvs, warnings = scan_library(tmp_path)
    assert cvs == [] and "zly.json" in warnings[0]


def test_folder_category():
    assert folder_category("DevOps") == "devops"
    assert folder_category("Administrator Linux") == "admin"
    assert folder_category("Administrator") == "admin"
    assert folder_category("helpdesk") == "helpdesk"
    assert folder_category("Python_Backend") == "python"
    assert folder_category("Różne") is None


def test_guess_language_and_clause():
    assert guess_language("Buduję projekty w Pythonie, uczę się Dockera") == "pl"
    assert guess_language("Projects and skills with Docker for the cloud and the web") == "en"
    assert strip_clause("Treść CV\nWyrażam zgodę na przetwarzanie") == "Treść CV"


def test_anonymize_removes_contact_data():
    text = "Jan Testowy\ntel. +48 500 000 000, jan@example.com\ngithub.com/example\nDocker"
    out = anonymize(text, ["Jan Testowy"])
    assert "Jan" not in out and "500" not in out and "@" not in out and "github" not in out
    assert "Docker" in out


# --- wynik dopasowania ---------------------------------------------------------------------


def test_offer_terms_skip_forbidden(profile):
    terms = offer_terms(devops_offer(), known_technologies(profile), exclude=["Azure"])
    assert "docker" in terms and "terraform" in terms and "aws" in terms
    assert "azure" not in terms


def test_rank_prefers_coverage_and_category(library, profile):
    cvs, _ = scan_library(library)
    ranked = rank(
        cvs,
        devops_offer(),
        vocabulary=known_technologies(profile),
        category="devops",
        exclude=["Azure"],
    )
    top = ranked[0]
    assert top.cv.key == "DevOps/cv-devops"
    assert top.coverage == 100 and top.category_match and top.score == 100
    assert ranked[1].score < 50 and "docker" in ranked[1].missing


def test_decide_by_score_thresholds():
    kw = {"use_threshold": 80, "edit_threshold": 50}
    assert decide_by_score(85, **kw) == "use"
    assert decide_by_score(60, **kw) == "edit"
    assert decide_by_score(10, **kw) == "new"


def test_script_decision_language_mismatch(library, profile, settings):
    cvs, _ = scan_library(library)
    offer = devops_offer(language="en")
    ranked = rank(cvs, offer, vocabulary=known_technologies(profile), category="devops")
    decision = script_decision(ranked, offer, settings)
    assert decision.action == "edit" and "innym języku" in decision.reason


# --- decyzja AI ------------------------------------------------------------------------------


def _ranked(library, profile, offer):
    cvs, _ = scan_library(library)
    return rank(cvs, offer, vocabulary=known_technologies(profile), category=offer.category)


def test_decide_ai_gets_only_top_candidates_without_contact(library, profile, settings):
    offer = devops_offer()
    ranked = _ranked(library, profile, offer)
    fake = FakeClaude([{"action": "edit", "cv": "Helpdesk/cv-helpdesk", "reason": "Lepsza baza"}])
    with use_runner(fake):
        decision = decide(ranked[:1], offer, settings, private=["Jan Testowy"])
    prompt = fake.calls[0][0]
    assert "DevOps/cv-devops" in prompt and "Helpdesk/cv-helpdesk" not in prompt
    # AI wskazało CV spoza listy → bierzemy najlepsze z listy.
    assert decision.action == "edit" and decision.candidate.cv.key == "DevOps/cv-devops"
    assert decision.by_ai and decision.notes


def test_decide_ai_prompt_is_anonymized(library, profile, settings):
    offer = devops_offer(title="Helpdesk IT", category="helpdesk", technologies=["Windows"])
    ranked = _ranked(library, profile, offer)
    fake = FakeClaude([{"action": "use", "cv": "Helpdesk/cv-helpdesk", "reason": "Pasuje"}])
    with use_runner(fake):
        decision = decide(ranked, offer, settings, private=["Jan Testowy", "+48 500 000 000"])
    prompt = fake.calls[0][0]
    assert "jan@example.com" not in prompt and "500 000" not in prompt
    assert "Jan Testowy" not in prompt and "pracodawce" not in prompt
    assert decision.action == "use" and decision.candidate.cv.key == "Helpdesk/cv-helpdesk"


def test_decide_falls_back_to_script_when_ai_fails(library, profile, settings):
    settings.claude_retries = 0
    offer = devops_offer()
    ranked = _ranked(library, profile, offer)
    with use_runner(FakeClaude([ClaudeError("limit")])):
        decision = decide(ranked, offer, settings)
    assert decision.action == "use" and not decision.by_ai
    assert "decyzja skryptu" in decision.notes[0]


def test_decide_without_candidates_skips_ai(settings):
    decision = decide([], devops_offer(), settings)  # atrapa claude rzuciłaby błąd
    assert decision.action == "new" and decision.candidate is None


# --- walidator tekstu ------------------------------------------------------------------------


def test_validate_text(profile):
    rules = [v.rule for v in validate_text("Azure, angielski płynny, DO UZUPEŁNIENIA", profile)]
    assert rules == ["forbidden-tech", "english-level", "unconfirmed"]
    assert validate_text("Docker, Linux, angielski B1", profile) == []


# --- dobór CV w pipeline ---------------------------------------------------------------------


@pytest.fixture
def engine(settings, profile):
    engine = make_engine(settings)
    init_db(engine)
    yield engine
    engine.dispose()


def add_offer(engine, **kw) -> int:
    with Session(engine) as session:
        offer = devops_offer(**kw)
        session.add(offer)
        session.commit()
        return offer.id


@pytest.fixture
def no_browser(monkeypatch):
    report = FitReport(
        main=ColumnFit(overflow_mm=0, free_mm=8), side=ColumnFit(overflow_mm=0, free_mm=2)
    )
    monkeypatch.setattr("cv_app.fit.measure_fit", lambda html, s: report)
    written: list[str] = []

    def fake_pdf(html, out, s):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF")
        written.append(html)
        return out

    monkeypatch.setattr("cv_app.render.render_pdf", fake_pdf)
    return written


def test_match_uses_ready_cv_from_json(settings, engine, library, no_browser):
    offer_id = add_offer(engine)
    log: list[str] = []
    fake = FakeClaude([{"action": "use", "cv": "DevOps/cv-devops", "reason": "Pasuje w 100%"}])
    with use_runner(fake):
        report = match_cv(settings, engine, offer_id, log.append)
    assert len(fake.calls) == 1  # tylko decyzja, bez generowania
    assert report.action == "use" and report.cv == "DevOps/cv-devops" and report.score == 100
    assert preview_path(settings, offer_id).is_file()
    assert json.loads(blocks_path(settings, offer_id).read_text())["title"] == blocks().title
    preview = read_preview_report(settings, offer_id)
    assert preview.ok and preview.source == "gotowe CV: DevOps/cv-devops"
    saved = read_match_report(settings, offer_id)
    assert saved.reason == "Pasuje w 100%" and len(saved.candidates) == 2


def test_match_copies_pdf_only_cv(settings, engine, library, no_browser):
    offer_id = add_offer(engine, title="Helpdesk IT", category="helpdesk", technologies=[])
    fake = FakeClaude([{"action": "use", "cv": "Helpdesk/cv-helpdesk", "reason": "Pasuje"}])
    with use_runner(fake):
        report = match_cv(settings, engine, offer_id, lambda _: None)
    assert report.action == "use"
    assert (
        preview_path(settings, offer_id).read_bytes()
        == (library / "Helpdesk" / "cv-helpdesk.pdf").read_bytes()
    )
    assert no_browser == []  # nic nie renderowano


def test_match_edits_ready_cv_that_breaks_rules(settings, engine, library, no_browser):
    bad = blocks(skill_groups=[{"name": "Cloud", "skills": ["Docker", "AWS", "Azure"]}])
    (library / "DevOps" / "cv-devops.json").write_text(bad.model_dump_json(), encoding="utf-8")
    offer_id = add_offer(engine)
    fake = FakeClaude([{"action": "use", "cv": "DevOps/cv-devops", "reason": "Pasuje"}, answer()])
    with use_runner(fake):
        report = match_cv(settings, engine, offer_id, lambda _: None)
    assert report.action == "edit" and "walidatora" in report.notes[-1]
    edit_prompt = fake.calls[1][0]
    assert "Azure" in edit_prompt and "Poprzednia wersja" in edit_prompt
    assert read_preview_report(settings, offer_id).source == "edytowane CV: DevOps/cv-devops"


def test_match_edits_pdf_only_cv_with_text_base(settings, engine, library, no_browser):
    offer_id = add_offer(engine, title="Helpdesk IT", category="helpdesk", technologies=["Bash"])
    fake = FakeClaude(
        [{"action": "edit", "cv": "Helpdesk/cv-helpdesk", "reason": "Dobra baza"}, answer()]
    )
    with use_runner(fake):
        report = match_cv(settings, engine, offer_id, lambda _: None)
    assert report.action == "edit"
    prompt = fake.calls[1][0]
    assert "Gotowe CV kandydata jako baza" in prompt and "Windows, Linux" in prompt
    assert "jan@example.com" not in prompt and "500 000" not in prompt


def test_match_creates_new_cv_without_library(settings, engine, no_browser):
    settings.cv_library_dir = settings.notes_dir.parent / "brak"
    offer_id = add_offer(engine)
    fake = FakeClaude([answer()])  # tylko generowanie, bez decyzji AI
    with use_runner(fake):
        report = match_cv(settings, engine, offer_id, lambda _: None)
    assert report.action == "new" and report.cv is None and len(fake.calls) == 1
    assert read_preview_report(settings, offer_id).ok


def test_match_unknown_offer(settings, engine):
    with pytest.raises(PipelineError):
        match_cv(settings, engine, 999, lambda _: None)


# --- panel ---------------------------------------------------------------------------------


def test_panel_cv_match_shows_choice(settings, profile, library, no_browser):
    from fastapi.testclient import TestClient

    from cv_app.app import create_app

    with TestClient(create_app(settings, jobs=JobRunner(background=False))) as client:
        _panel_check(client)


def _panel_check(client):
    engine = client.app.state.engine
    offer_id = add_offer(engine)
    fake = FakeClaude([{"action": "use", "cv": "DevOps/cv-devops", "reason": "Pasuje idealnie"}])
    with use_runner(fake):
        job = client.post(f"/api/offers/{offer_id}/cv-match").json()
    assert job["status"] == "done", job
    assert job["result"]["action"] == "use" and job["result"]["cv"] == "DevOps/cv-devops"
    html = client.get("/").text
    assert "Gotowe CV bez zmian" in html and "DevOps/cv-devops (100%)" in html
    assert "Pasuje idealnie" in html and "Porównane CV (2)" in html
    assert client.post("/api/offers/999/cv-match").status_code == 404
