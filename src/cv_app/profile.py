"""Parser profilu: ``O mnie.md`` i ``Projekty.md`` → modele Pydantic.

Parsowanie jest deterministyczne (bez AI). Pliki profilu są źródłem prawdy: aplikacja
czyta je przy każdym uruchomieniu i nie trzyma drugiej kopii danych.

Format (skrót):

- ``O mnie.md``: ``## Dane`` (lista ``- Klucz: wartość``), ``### Wariant ...`` (warianty
  podsumowania w cytacie ``>``), ``## Umiejętności`` (``- **Grupa:** a, b (x, y), c``),
  ``## Wykształcenie``, ``## Języki``.
- ``Projekty.md``: tabela „Jak dobierać projekty pod ofertę” i sekcje ``## Projekt``
  z polami ``**Link:**``, ``**Produkcyjnie:**``, ``**Na CV jako:**``, ``**Stack:**``,
  ``**Punkty do CV:**``.
- Callouty ``> [!important]`` w obu plikach to twarde reguły.
- Treść oznaczona „DO UZUPEŁNIENIA” / „DO POTWIERDZENIA” jest pomijana.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from cv_app.config import Settings

# Znaczniki niepewnych danych: takie pola i punkty pomijamy.
UNCONFIRMED_MARKERS = ("DO UZUPEŁNIENIA", "DO POTWIERDZENIA")
# „[DO POTWIERDZENIA: relokacja?]” to pytanie o dodatkową informację: dopisek usuwamy,
# a reszta pola zostaje. Goły znacznik („Jenkins [DO POTWIERDZENIA]”) dotyczy całego pola.
_BRACKETED_MARKER = re.compile(r"\s*\[(?:DO UZUPEŁNIENIA|DO POTWIERDZENIA):[^\]]*\]", re.I)

# Dopiski, które muszą zostać przy umiejętności.
QUALIFIER_WORDS = ("podstawy", "teoria", "basics")

_CALLOUT = re.compile(r"^\[!(\w+)\]\s*(.*)$")
_FIELD = re.compile(r"\*\*(.+?):\*\*\s*(.*)")


class ProfileError(Exception):
    """Brak plików profilu albo ich format nie pozwala na odczyt."""


# --- modele -----------------------------------------------------------------------


class Rule(BaseModel):
    """Twarda reguła z calloutu ``[!important]``."""

    text: str
    source: str  # plik, z którego pochodzi
    section: str  # nagłówek sekcji (np. "Umiejętności" albo nazwa projektu)


class Contact(BaseModel):
    name: str
    phone: str | None = None
    email: str | None = None
    location: str | None = None
    github: str | None = None
    gitlab: str | None = None
    linkedin: str | None = None
    website: str | None = None
    photo: bool = False


class SummaryVariant(BaseModel):
    name: str  # np. "DevOps / Cloud"
    text: str


class Skill(BaseModel):
    name: str  # dokładnie tak, jak w profilu, np. "podstawy Ansible (teoria)"
    qualifiers: list[str] = Field(default_factory=list)  # np. ["podstawy", "teoria"]


class SkillGroup(BaseModel):
    name: str
    skills: list[Skill]


class Project(BaseModel):
    key: str  # nazwa z nagłówka ``## ``, np. "YTLottery"
    subtitle: str | None = None
    cv_name: str | None = None  # „Na CV jako”
    link: str | None = None
    production: str | None = None
    stack: list[str] = Field(default_factory=list)
    points: list[str] = Field(default_factory=list)
    description: str = ""
    rules: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # callouty [!note]
    ai_assisted: bool = False  # AI-asystę trzeba podać wprost
    standalone: bool = True  # False: nie wpisywać jako osobnego projektu
    merged_into: str | None = None  # np. AWS-project-01 → terraform-3tier

    @property
    def display_name(self) -> str:
        return self.cv_name or self.key


class PriorityItem(BaseModel):
    project: str
    hint: str | None = None  # dopisek w nawiasie, np. "monitoring, runbook"


class Priority(BaseModel):
    offer_type: str  # np. "DevOps / Cloud"
    projects: list[PriorityItem]


class Language(BaseModel):
    name: str
    level: str
    note: str | None = None


class Profile(BaseModel):
    contact: Contact
    variants: list[SummaryVariant]
    skill_groups: list[SkillGroup]
    education: list[str]
    languages: list[Language]
    projects: list[Project]
    priorities: list[Priority]
    rules: list[Rule]
    version: str  # skrót z treści plików; zmienia się po każdej edycji profilu

    def project(self, name: str) -> Project | None:
        """Projekt po kluczu albo nazwie z CV (bez rozróżniania wielkości liter)."""
        wanted = name.strip().lower()
        for p in self.projects:
            if wanted in (p.key.lower(), (p.cv_name or "").lower()):
                return p
        return None

    def variant(self, name: str) -> SummaryVariant | None:
        wanted = name.strip().lower()
        return next((v for v in self.variants if v.name.lower() == wanted), None)

    @property
    def all_skills(self) -> list[Skill]:
        return [s for g in self.skill_groups for s in g.skills]

    @property
    def cv_projects(self) -> list[Project]:
        """Projekty, które mogą być osobną pozycją w CV."""
        return [p for p in self.projects if p.standalone]


# --- markdown: sekcje, callouty, listy ----------------------------------------------


@dataclass
class _Section:
    level: int
    title: str
    lines: list[str] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)

    def child(self, prefix: str) -> _Section | None:
        p = prefix.lower()
        return next((c for c in self.children if c.title.lower().startswith(p)), None)

    def walk(self) -> Iterator[_Section]:
        yield self
        for c in self.children:
            yield from c.walk()


def _parse_sections(text: str) -> _Section:
    root = _Section(level=0, title="")
    stack = [root]
    in_code = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            while stack[-1].level >= level:
                stack.pop()
            sec = _Section(level=level, title=m.group(2).strip())
            stack[-1].children.append(sec)
            stack.append(sec)
        else:
            stack[-1].lines.append(line)
    return root


def _blockquotes(lines: list[str]) -> Iterator[list[str]]:
    """Kolejne bloki cytatu ``>`` (bez znaku ``>``)."""
    block: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith(">"):
            block.append(s[1:].strip())
        elif block:
            yield block
            block = []
    if block:
        yield block


def _callouts(lines: list[str]) -> Iterator[tuple[str, str]]:
    """Callouty Obsidiana: ``(typ, treść)``."""
    for block in _blockquotes(lines):
        m = _CALLOUT.match(block[0])
        if m:
            body = " ".join([m.group(2), *block[1:]]).strip()
            yield m.group(1).lower(), _clean_inline(body)


def _bullets(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _clean_inline(text: str) -> str:
    """Usuwa formatowanie markdown, które nie powinno trafić do CV."""
    text = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_unconfirmed(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in UNCONFIRMED_MARKERS)


def _drop_unconfirmed(text: str) -> str | None:
    """Usuwa dopiski ``[DO POTWIERDZENIA: ...]``; całe pole bez potwierdzenia → None."""
    cleaned = _BRACKETED_MARKER.sub("", text).strip()
    if not cleaned or _is_unconfirmed(cleaned):
        return None
    return cleaned


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Dzieli po przecinkach poza nawiasami: ``AWS (EC2, VPC), Git`` → 2 elementy."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        if ch == sep and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def _qualifiers(skill: str) -> list[str]:
    low = skill.lower()
    found = [q for q in QUALIFIER_WORDS if re.search(rf"\b{q}\b", low)]
    return ["podstawy" if q == "basics" else q for q in found]


# --- O mnie.md -----------------------------------------------------------------------

_CONTACT_KEYS = {
    "imię i nazwisko": "name",
    "telefon": "phone",
    "e-mail": "email",
    "email": "email",
    "lokalizacja": "location",
    "github": "github",
    "gitlab": "gitlab",
    "linkedin": "linkedin",
    "strona cv": "website",
    "strona": "website",
    "zdjęcie": "photo",
}


def _parse_contact(section: _Section | None) -> Contact:
    if section is None:
        raise ProfileError("W pliku „O mnie.md” brakuje sekcji „## Dane”.")
    values: dict[str, object] = {}
    for item in _bullets(section.lines):
        key, sep, value = item.partition(":")
        if not sep:
            continue
        target = _CONTACT_KEYS.get(key.strip().lower())
        cleaned = _drop_unconfirmed(_clean_inline(value))
        if target is None or cleaned is None:
            continue
        if target == "photo":
            values["photo"] = cleaned.lower().startswith("tak")
        else:
            values[target] = cleaned
    if "name" not in values:
        raise ProfileError("W sekcji „## Dane” brakuje pola „Imię i nazwisko”.")
    return Contact(**values)  # type: ignore[arg-type]


def _parse_variants(section: _Section | None) -> list[SummaryVariant]:
    variants: list[SummaryVariant] = []
    for sub in section.children if section else []:
        m = re.match(r"^Wariant\s+(.*)$", sub.title, re.I)
        if not m:
            continue
        texts = []
        for block in _blockquotes(sub.lines):
            head = _CALLOUT.match(block[0])
            if head and head.group(1).lower() == "important":
                continue  # reguła, nie treść podsumowania
            body = block[1:] if head else block
            if body:
                texts.append(" ".join(body))
        text = _clean_inline(" ".join(texts))
        if text and not _is_unconfirmed(text):
            variants.append(SummaryVariant(name=m.group(1).strip(), text=text))
    return variants


def _parse_skills(section: _Section | None) -> list[SkillGroup]:
    groups: list[SkillGroup] = []
    for item in _bullets(section.lines if section else []):
        m = _FIELD.match(item)
        if not m:
            continue
        skills = []
        for raw in split_top_level(m.group(2)):
            name = _drop_unconfirmed(_clean_inline(raw.replace("`", "")))
            if name:
                skills.append(Skill(name=name, qualifiers=_qualifiers(name)))
        if skills:
            groups.append(SkillGroup(name=m.group(1).strip(), skills=skills))
    return groups


def _parse_languages(section: _Section | None) -> list[Language]:
    out = []
    for item in _bullets(section.lines if section else []):
        parts = re.split(r"\s+[—–-]\s+", item, maxsplit=1)
        if len(parts) != 2:
            continue
        rest = _clean_inline(parts[1])
        level, _, note = rest.partition(" (")
        out.append(
            Language(name=parts[0].strip(), level=level.strip(), note=note.rstrip(")") or None)
        )
    return out


# --- Projekty.md ---------------------------------------------------------------------


def _parse_priorities(section: _Section | None) -> list[Priority]:
    out: list[Priority] = []
    for line in section.lines if section else []:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not line.strip().startswith("|"):
            continue
        if set(cells[0]) <= set("-: ") or cells[0].lower().startswith("oferta na"):
            continue
        items = []
        for raw in split_top_level(cells[1]):
            m = re.match(r"^(.*?)\s*\((.*)\)\s*$", raw)
            name, hint = (m.group(1), m.group(2)) if m else (raw, None)
            items.append(PriorityItem(project=name.strip(), hint=hint))
        out.append(Priority(offer_type=_clean_inline(cells[0]), projects=items))
    return out


def _parse_project(sec: _Section) -> Project:
    key, _, subtitle = (part.strip() for part in sec.title.partition(" — "))
    fields: dict[str, str] = {}
    points: list[str] = []
    description: list[str] = []
    in_points = False
    for line in sec.lines:
        s = line.strip()
        if not s or s == "---" or s.startswith(">"):
            continue
        if s.startswith("**"):
            in_points = False
            for part in s.split(" | "):
                m = _FIELD.match(part.strip())
                if m:
                    fields[m.group(1).strip().lower()] = m.group(2).strip()
                    if m.group(1).strip().lower() == "punkty do cv":
                        in_points = True
            continue
        if in_points and re.match(r"^[-*]\s+", s):
            point = _drop_unconfirmed(_clean_inline(re.sub(r"^[-*]\s+", "", s)))
            if point:
                points.append(point.replace("`", ""))
            continue
        in_points = False
        if not s.startswith(("-", "*")):
            description.append(s)

    rules, notes = [], []
    for kind, body in _callouts(sec.lines):
        if kind == "important":
            rules.append(body)
        elif kind == "note":
            notes.append(body)
    joined = " ".join(rules).lower()
    merged = re.search(r"patrz\s+([\w.-]+)", " ".join(rules), re.I)

    def field_value(name: str) -> str | None:
        value = fields.get(name)
        return _drop_unconfirmed(_clean_inline(value)) if value else None

    stack = field_value("stack")
    return Project(
        key=key,
        subtitle=subtitle or None,
        cv_name=field_value("na cv jako"),
        link=field_value("link"),
        production=field_value("produkcyjnie"),
        stack=split_top_level(stack) if stack else [],
        points=points,
        description=_clean_inline(" ".join(description)),
        rules=rules,
        notes=notes,
        ai_assisted="ai-asyst" in joined,
        standalone=not re.search(r"nie jako osobny projekt|nie wpisywać do cv osobno", joined),
        merged_into=merged.group(1).rstrip(".") if merged else None,
    )


def _is_project_section(sec: _Section) -> bool:
    return any(re.match(r"^\*\*(Link|Stack):\*\*", line.strip()) for line in sec.lines)


# --- wejście ---------------------------------------------------------------------------


def _collect_rules(root: _Section, source: str) -> list[Rule]:
    rules = []
    for sec in root.walk():
        for kind, body in _callouts(sec.lines):
            if kind == "important" and body:
                rules.append(Rule(text=body, source=source, section=sec.title or source))
    return rules


def parse_profile(about_text: str, projects_text: str) -> Profile:
    about = _parse_sections(about_text)
    projects_root = _parse_sections(projects_text)
    about_top = about.children[0] if about.children and about.children[0].level == 1 else about
    proj_top = (
        projects_root.children[0]
        if projects_root.children and projects_root.children[0].level == 1
        else projects_root
    )

    projects = [_parse_project(s) for s in proj_top.children if _is_project_section(s)]
    if not projects:
        raise ProfileError("W pliku „Projekty.md” nie znaleziono żadnego projektu.")

    digest = hashlib.sha256((about_text + "\0" + projects_text).encode()).hexdigest()[:12]
    return Profile(
        contact=_parse_contact(about_top.child("Dane")),
        variants=_parse_variants(about_top.child("Profil zawodowy")),
        skill_groups=_parse_skills(about_top.child("Umiejętności")),
        education=[
            c
            for c in (
                _drop_unconfirmed(_clean_inline(b))
                for b in _bullets((about_top.child("Wykształcenie") or _Section(0, "")).lines)
            )
            if c
        ],
        languages=_parse_languages(about_top.child("Języki")),
        projects=projects,
        priorities=_parse_priorities(proj_top.child("Jak dobierać")),
        rules=_collect_rules(about, "O mnie.md") + _collect_rules(projects_root, "Projekty.md"),
        version=digest,
    )


def profile_paths(settings: Settings) -> tuple[Path, Path]:
    return (
        settings.notes_dir / settings.profile_about_file,
        settings.notes_dir / settings.profile_projects_file,
    )


def load_profile(settings: Settings) -> Profile:
    """Czyta profil z ``CV_APP_NOTES_DIR``. Zawsze od nowa, bez cache."""
    about_path, projects_path = profile_paths(settings)
    missing = [p for p in (about_path, projects_path) if not p.is_file()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise ProfileError(f"Nie znaleziono plików profilu: {names}")
    return parse_profile(
        about_path.read_text(encoding="utf-8"), projects_path.read_text(encoding="utf-8")
    )
