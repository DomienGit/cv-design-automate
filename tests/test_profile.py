from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cv_app.config import Settings
from cv_app.profile import ProfileError, load_profile, parse_profile, split_top_level

FIXTURES = Path(__file__).parent / "fixtures" / "profile_sample"


@pytest.fixture
def profile_settings(settings: Settings) -> Settings:
    shutil.copytree(FIXTURES, settings.notes_dir)
    return settings


@pytest.fixture
def profile(profile_settings: Settings):
    return load_profile(profile_settings)


def test_contact_drops_unconfirmed_notes(profile):
    c = profile.contact
    assert c.name == "Jan Testowy"
    assert c.location == "Testowo (00-001), Polska"
    assert c.github == "https://github.com/example"
    assert c.website == "https://example.com"
    assert c.photo is True


def test_variants_skip_callout_header_and_unconfirmed(profile):
    names = [v.name for v in profile.variants]
    assert names == ["DevOps / Cloud", "Helpdesk / Wsparcie IT"]
    assert profile.variant("helpdesk / wsparcie it").text == (
        "Technik informatyk znający Windows i Linuxa."
    )


def test_skills_keep_qualifiers_and_nested_commas(profile):
    groups = {g.name: g for g in profile.skill_groups}
    devops = [s.name for s in groups["DevOps / Cloud"].skills]
    assert devops == [
        "Docker",
        "AWS (EC2, VPC, S3)",
        "podstawy Kubernetes",
        "podstawy Ansible (teoria)",
    ]
    quals = {s.name: s.qualifiers for s in profile.all_skills}
    assert quals["C (podstawy)"] == ["podstawy"]
    assert quals["podstawy Ansible (teoria)"] == ["podstawy", "teoria"]
    assert [s.name for s in groups["Narzędzia"].skills] == ["pytest", "Git"]


def test_projects_fields_points_and_flags(profile):
    faktury = profile.project("faktury ai")
    assert faktury.link == "https://github.com/example/faktury"
    assert faktury.production == "faktury.example.com"
    assert faktury.stack == ["Python 3.12", "Flask", "Docker Compose (Caddy, TLS)"]
    assert faktury.points == [
        "Zbudowałem z wykorzystaniem narzędzi AI system odczytu faktur",
        "Wdrożenie na VPS z Docker Compose",
    ]
    assert faktury.ai_assisted is True
    assert faktury.description == "System odczytu faktur PDF."

    old = profile.project("infra-stare")
    assert old.standalone is False and old.merged_into == "infra-demo"
    assert profile.project("sieciak-api").standalone is False
    assert [p.key for p in profile.cv_projects] == ["Faktury AI", "infra-demo"]


def test_priorities_table(profile):
    helpdesk = profile.priorities[1]
    assert helpdesk.offer_type == "Helpdesk / Wsparcie IT"
    assert [(i.project, i.hint) for i in helpdesk.projects] == [
        ("Faktury AI", "monitoring, runbook"),
        ("sieciak", "sieci"),
    ]


def test_rules_from_important_callouts(profile):
    sections = [r.section for r in profile.rules]
    assert "Umiejętności" in sections
    assert "Doświadczenie zawodowe" in sections
    assert any("Azure" in r.text for r in profile.rules)
    assert any(r.source == "Projekty.md" and "dat" in r.text for r in profile.rules)


def test_education_and_languages(profile):
    assert profile.education[0] == "Technikum Testowe, profil informatyczny"
    en = profile.languages[1]
    assert (en.name, en.level) == ("angielski", "B1")
    assert "płynności" in en.note


def test_version_changes_with_content(profile_settings):
    v1 = load_profile(profile_settings).version
    about = profile_settings.notes_dir / "O mnie.md"
    about.write_text(about.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert load_profile(profile_settings).version != v1


def test_missing_files_raise_readable_error(settings):
    with pytest.raises(ProfileError, match="Nie znaleziono"):
        load_profile(settings)


def test_no_projects_is_an_error():
    with pytest.raises(ProfileError):
        parse_profile("# O mnie\n\n## Dane\n\n- Imię i nazwisko: X\n", "# Projekty\n")


def test_split_top_level():
    assert split_top_level("a, b (c, d), e [f, g]") == ["a", "b (c, d)", "e [f, g]"]


REAL_DIR = Path(__file__).resolve().parents[1] / "profil"


def test_real_profile_parses():
    """Prawdziwe pliki Dominika: tylko lokalnie, w CI ich nie ma."""
    if not (REAL_DIR / "O mnie.md").is_file() or not (REAL_DIR / "Projekty.md").is_file():
        pytest.skip("brak prawdziwych plików profilu w ./profil")
    p = load_profile(Settings(notes_dir=REAL_DIR))
    assert p.contact.name
    assert len(p.variants) >= 3
    assert p.project("terraform-3tier") is not None
    aws = p.project("AWS-project-01")
    if aws is not None:
        assert not aws.standalone and aws.merged_into == "terraform-3tier"
    # Azure nie może być w puli umiejętności.
    assert not any("azure" in s.name.lower() for s in p.all_skills)
