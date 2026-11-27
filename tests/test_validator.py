"""Walidator reguł CV i pętla poprawek w podglądzie CV."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlmodel import Session
from test_generator import answer

from cv_app.claude_client import FakeClaude, use_runner
from cv_app.cv_blocks import CVBlocks, EducationBlock, LanguageBlock, ProjectBlock, SkillGroupBlock
from cv_app.db import init_db, make_engine
from cv_app.fit import ColumnFit, FitReport
from cv_app.models import Offer
from cv_app.pipeline import PipelineError, preview_cv, read_preview_report
from cv_app.profile import load_profile
from cv_app.validator import forbidden_technologies, validate

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


@pytest.fixture
def profile(settings):
    shutil.copytree(FIXTURES, settings.notes_dir)
    return load_profile(settings)


def blocks(**kw) -> CVBlocks:
    data = {
        "language": "pl",
        "title": "Junior DevOps Engineer",
        "summary": "Programista Python rozwijający się w kierunku DevOps.",
        "skill_groups": [
            SkillGroupBlock(
                name="DevOps", skills=["Docker", "AWS (EC2, VPC)", "podstawy Kubernetes"]
            ),
            SkillGroupBlock(name="Języki", skills=["Python", "Bash", "C (podstawy)"]),
        ],
        "projects": [
            ProjectBlock(
                name="infra-demo",
                tags=["AWS", "Terraform"],
                points=["Odtworzyłem architekturę 3-warstwową", "Całość w Terraformie"],
            ),
            ProjectBlock(
                name="Faktury AI",
                tags=["Flask"],
                points=["Zbudowałem z użyciem narzędzi AI system odczytu faktur", "Wdrożenie"],
            ),
        ],
        "education": [EducationBlock(school="Technikum Testowe")],
        "languages": [
            LanguageBlock(name="Polski", level="ojczysty"),
            LanguageBlock(name="Angielski", level="B1"),
        ],
    }
    data.update(kw)
    return CVBlocks(**data)


def rules(violations) -> list[str]:
    return [v.rule for v in violations]


def test_clean_cv_passes(profile):
    assert validate(blocks(), profile) == []


def test_forbidden_tech_read_from_profile(profile):
    assert forbidden_technologies(profile) == ["Azure"]
    cv = blocks(summary="Znam AWS i Azure.")
    found = validate(cv, profile)
    assert rules(found) == ["forbidden-tech"]
    assert "Azure" in found[0].message


def test_forbidden_tech_in_skills_reported_once(profile):
    cv = blocks(skill_groups=[SkillGroupBlock(name="Cloud", skills=["Docker", "Azure"])])
    assert rules(validate(cv, profile)) == ["forbidden-tech"]


def test_english_level_and_fluency(profile):
    cv = blocks(
        summary="Płynny angielski i Docker.",
        languages=[LanguageBlock(name="Angielski", level="C1")],
    )
    found = validate(cv, profile)
    assert rules(found) == ["english-level", "english-level"]
    assert "B1" in found[1].message
    # „płynne animacje” to nie deklaracja poziomu języka
    assert validate(blocks(summary="Tworzę płynne animacje w Pythonie."), profile) == []


def test_english_cv_fluent(profile):
    cv = blocks(language="en", summary="Fluent in English.")
    assert "english-level" in rules(validate(cv, profile))


def test_skill_qualifiers_required(profile):
    cv = blocks(
        skill_groups=[SkillGroupBlock(name="DevOps", skills=["Docker", "Kubernetes", "Ansible"])]
    )
    found = validate(cv, profile)
    assert rules(found) == ["skill-qualifier", "skill-qualifier"]
    assert "„podstawy”" in found[0].message
    en = blocks(
        language="en",
        skill_groups=[SkillGroupBlock(name="DevOps", skills=["Kubernetes (basics)", "Ansible"])],
    )
    found = validate(en, profile)
    assert rules(found) == ["skill-qualifier"]
    assert "„basics”" in found[0].message


def test_unknown_skill(profile):
    cv = blocks(skill_groups=[SkillGroupBlock(name="Cloud", skills=["Docker", "Google Cloud"])])
    found = validate(cv, profile)
    assert rules(found) == ["unknown-skill"]
    # skrót nazwy z profilu, technologia ze stacku projektu i fraza z opisu profilu
    ok = blocks(
        skill_groups=[
            SkillGroupBlock(name="Cloud", skills=["Docker Compose", "AWS EC2", "Flask", "Git"])
        ]
    )
    assert validate(ok, profile) == []


def test_translated_soft_skill_in_english_cv_is_allowed(profile):
    cv = blocks(
        language="en",
        skill_groups=[SkillGroupBlock(name="Other", skills=["Docker", "Computer repair"])],
    )
    assert validate(cv, profile) == []


def test_ai_assisted_project(profile):
    cv = blocks(
        projects=[
            ProjectBlock(name="Faktury AI", points=["Napisałem ręcznie system faktur", "Docker"])
        ]
    )
    found = validate(cv, profile)
    assert rules(found) == ["ai-assisted", "ai-assisted"]


def test_merged_and_unknown_projects(profile):
    cv = blocks(
        projects=[
            ProjectBlock(name="infra-stare", points=["a", "b"]),
            ProjectBlock(name="sieciak-api", points=["a", "b"]),
            ProjectBlock(name="Nieistniejący", points=["a", "b"]),
        ]
    )
    assert rules(validate(cv, profile)) == ["merged-project", "merged-project", "unknown-project"]


def test_project_dates(profile):
    cv = blocks(
        projects=[ProjectBlock(name="infra-demo", points=["Odtworzyłem w 2025", "Python 3.12"])]
    )
    assert rules(validate(cv, profile)) == ["project-dates"]
    cv = blocks(projects=[ProjectBlock(name="infra-demo", points=["W listopadzie", "Terraform"])])
    assert rules(validate(cv, profile)) == ["project-dates"]


def test_project_specific_rules_from_profile(profile):
    infra = profile.project("infra-demo")
    infra.rules.append('Na CV mówić wprost, np. "odtworzyłem".')
    faktury = profile.project("Faktury AI")
    faktury.rules.append("Nie pisać o użytkownikach, ruchu ani liczbach użycia.")
    cv = blocks(
        projects=[
            ProjectBlock(name="infra-demo", points=["Architektura 3-warstwowa", "Terraform"]),
            ProjectBlock(
                name="Faktury AI",
                points=["Z użyciem narzędzi AI zbudowałem system", "Obsługuje 50 użytkowników"],
            ),
        ]
    )
    assert rules(validate(cv, profile)) == ["rebuilt-project", "usage-numbers"]
    # „uruchomiłem” nie jest „ruchem”
    ok = blocks(
        projects=[
            ProjectBlock(name="infra-demo", points=["Odtworzyłem architekturę", "Terraform"]),
            ProjectBlock(name="Faktury AI", points=["Z użyciem narzędzi AI uruchomiłem", "VPS"]),
        ]
    )
    assert validate(ok, profile) == []


def test_experience_work_terms_and_markers(profile):
    cv = blocks(
        title="DevOps (praca zdalna)",
        summary="Mam kilkuletnie doświadczenie. DO POTWIERDZENIA",
    )
    assert rules(validate(cv, profile)) == ["experience", "work-terms", "unconfirmed"]


# --- pętla poprawek ----------------------------------------------------------------------


@pytest.fixture
def offer_id(settings, profile):
    engine = make_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        session.add(Offer(source="pracuj", external_id="1", title="Junior DevOps", id=7))
        session.commit()
    yield 7
    engine.dispose()


@pytest.fixture
def no_browser(monkeypatch):
    report = FitReport(
        main=ColumnFit(overflow_mm=0, free_mm=8), side=ColumnFit(overflow_mm=0, free_mm=2)
    )
    monkeypatch.setattr("cv_app.fit.measure_fit", lambda html, s: report)
    written = []

    def fake_pdf(html, out, s):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF")
        written.append(html)
        return out

    monkeypatch.setattr("cv_app.render.render_pdf", fake_pdf)
    return written


def _bad_answer():
    bad = answer()
    bad["skill_groups"][0]["skills"].append("Azure")
    return bad


def test_fix_loop_corrects_cv(settings, offer_id, no_browser):
    log: list[str] = []
    fake = FakeClaude([_bad_answer(), answer()])
    with use_runner(fake):
        preview_cv(settings, make_engine(settings), offer_id, log.append)
    assert len(fake.calls) == 2
    second_prompt = fake.calls[1][0]
    assert "Poprawki do poprzedniej wersji" in second_prompt and "Azure" in second_prompt
    assert "Poprzednia wersja" in second_prompt
    report = read_preview_report(settings, offer_id)
    assert report.ok and report.fixes == 1 and report.problems == []
    assert "Azure" not in no_browser[-1]
    assert any("poprawka 1/3" in line for line in log)


def test_fix_loop_gives_up_with_warning(settings, offer_id, no_browser):
    settings.cv_fix_attempts = 2
    log: list[str] = []
    fake = FakeClaude([_bad_answer() for _ in range(3)])
    with use_runner(fake):
        out = preview_cv(settings, make_engine(settings), offer_id, log.append)
    assert len(fake.calls) == 3 and out.is_file()
    report = read_preview_report(settings, offer_id)
    assert not report.ok and report.fixes == 2
    assert any("Azure" in p for p in report.problems)
    assert any(line.startswith("UWAGA") for line in log)


def test_fix_loop_keeps_previous_version_when_fix_fails(settings, offer_id, no_browser):
    from cv_app.claude_client import ClaudeError

    settings.claude_retries = 0
    fake = FakeClaude([_bad_answer(), ClaudeError("limit")])
    with use_runner(fake):
        preview_cv(settings, make_engine(settings), offer_id, lambda _: None)
    report = read_preview_report(settings, offer_id)
    assert not report.ok and report.fixes == 0


def test_first_generation_error_is_reported(settings, offer_id, no_browser):
    from cv_app.claude_client import ClaudeError

    settings.claude_retries = 0
    with use_runner(FakeClaude([ClaudeError("brak claude")])), pytest.raises(PipelineError):
        preview_cv(settings, make_engine(settings), offer_id, lambda _: None)
