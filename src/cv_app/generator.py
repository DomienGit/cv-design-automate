"""Generator bloków CV: oferta + profil → bloki CV (JSON) przez Claude.

Podział pracy:

- lokalnie (deterministycznie): kategoria oferty → wariant podsumowania i kolejność
  projektów z tabeli „Jak dobierać projekty pod ofertę”, lista dozwolonych projektów,
  nazwy i linki projektów, języki, tagi tylko ze stacku projektu;
- AI (``run_claude``): wybór projektów, punktów i umiejętności z profilu, skrócenie
  i przeformułowanie pod ofertę, tłumaczenie na angielski dla ofert po angielsku.

Do promptu nie trafiają dane kontaktowe (telefon, e-mail, adres, linki): szablon
wstawia je lokalnie przy renderze. AI dostaje twarde reguły z calloutów
``[!important]`` i limity długości; pełne sprawdzenie reguł robi walidator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, create_model

from cv_app.claude_client import run_claude
from cv_app.config import Settings, get_settings
from cv_app.cv_blocks import (
    HARD_MAX,
    TARGET,
    CVBlocks,
    EducationBlock,
    LanguageBlock,
    ProjectBlock,
    SkillGroupBlock,
)
from cv_app.cv_blocks import Language as CVLanguage
from cv_app.models import CV, CVStatus, Offer
from cv_app.profile import Priority, Profile, Project, SummaryVariant
from cv_app.relevance import classify_title, normalize

# Kategoria oferty (z filtra trafności) → słowa w nazwie wariantu i wiersza tabeli
# priorytetów w profilu. Nazwy wariantów zostają w profilu, tu tylko ich dopasowanie.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "admin": ("administrator", "admin", "linux"),
    "devops": ("devops", "cloud"),
    "helpdesk": ("helpdesk", "wsparcie"),
    "python": ("python", "backend"),
    "ai": ("ai", "automatyzacje"),
}

# Sformułowania sugerujące pracę zawodową. Profil mówi: brak doświadczenia komercyjnego,
# obecna praca (fizyczna) nie trafia do CV. Słowa sprawdzamy w tekście bazowym
# i w odpowiedzi AI.
EXPERIENCE_PHRASES: tuple[str, ...] = (
    "współpracowni",
    "kolegom z pracy",
    "w mojej pracy",
    "w obecnej pracy",
    "w poprzedniej firmie",
    "w firmie",
    "pracodaw",
    "doświadczenie zawodowe",
    "doświadczenie komercyjne",
    "kilkuletnie doświadczenie",
    "lat doświadczenia",
    "coworker",
    "co-worker",
    "colleague",
    "at work",
    "my employer",
    "previous company",
    "professional experience",
    "commercial experience",
    "years of experience",
)

_COWORKERS = re.compile(
    r"(?P<before>\s*(?:,|\bi\b|\boraz\b)\s*)?\bwspółpracownik\w*"
    r"(?P<after>\s*(?:,|\bi\b|\boraz\b)\s*)?",
    re.IGNORECASE,
)

LANGUAGE_NAMES_EN = {
    "polski": "Polish",
    "angielski": "English",
    "niemiecki": "German",
    "hiszpański": "Spanish",
    "francuski": "French",
    "rosyjski": "Russian",
    "ukraiński": "Ukrainian",
}
LEVELS_EN = {"ojczysty": "native", "podstawowy": "basic", "komunikatywny": "conversational"}


class GeneratorError(Exception):
    """Nie udało się przygotować bloków CV."""


# --- wybór wariantu i projektów --------------------------------------------------------


def _matches(name: str, keywords: tuple[str, ...]) -> bool:
    text = normalize(name)
    return any(re.search(rf"\b{re.escape(normalize(k))}\b", text) for k in keywords)


def offer_category(offer: Offer, settings: Settings) -> str | None:
    if offer.category:
        return offer.category
    return classify_title(offer.title, settings).category


def pick_variant(profile: Profile, category: str | None) -> SummaryVariant:
    if not profile.variants:
        raise GeneratorError("Profil nie ma żadnego wariantu podsumowania (### Wariant ...).")
    keywords = CATEGORY_KEYWORDS.get(category or "", ())
    return next((v for v in profile.variants if _matches(v.name, keywords)), profile.variants[0])


def pick_priority(profile: Profile, category: str | None) -> Priority | None:
    keywords = CATEGORY_KEYWORDS.get(category or "", ())
    return next((p for p in profile.priorities if _matches(p.offer_type, keywords)), None)


@dataclass
class Candidate:
    project: Project
    priority: int | None  # miejsce w tabeli priorytetów (1 = najważniejszy)
    hint: str | None = None  # dopisek z tabeli, np. "monitoring, runbook"


def candidate_projects(profile: Profile, category: str | None) -> list[Candidate]:
    """Projekty, które mogą być w CV: najpierw priorytetowe dla kategorii, potem reszta."""
    out: list[Candidate] = []
    seen: set[str] = set()
    priority = pick_priority(profile, category)
    for i, item in enumerate(priority.projects if priority else [], start=1):
        project = profile.project(item.project)
        if project and project.standalone and project.key not in seen and project.points:
            out.append(Candidate(project, i, item.hint))
            seen.add(project.key)
    for project in profile.cv_projects:
        if project.key not in seen and project.points:
            out.append(Candidate(project, None))
            seen.add(project.key)
    return out


# --- tekst wejściowy -------------------------------------------------------------------


def sanitize_source_text(text: str) -> str:
    """Usuwa z tekstu bazowego wzmianki sugerujące pracę zawodową.

    Np. „pomagając współpracownikom i znajomym” → „pomagając znajomym”: pomoc
    znajomym to fakt z profilu, a współpracownicy sugerowaliby doświadczenie w IT.
    """

    def repl(m: re.Match[str]) -> str:
        # „znajomym, współpracownikom i rodzinie” → „znajomym, rodzinie”
        if m.group("before") and m.group("after"):
            return m.group("before")
        return " "

    cleaned = _COWORKERS.sub(repl, text)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    return " ".join(cleaned.split())


def experience_claims(text: str) -> list[str]:
    """Sformułowania z :data:`EXPERIENCE_PHRASES` znalezione w tekście."""
    low = text.lower()
    return [p for p in EXPERIENCE_PHRASES if p in low]


def blocks_text(blocks: CVBlocks) -> str:
    parts = [blocks.title, blocks.summary]
    for project in blocks.projects:
        parts += project.points
    for edu in blocks.education:
        parts += [edu.school, edu.subtitle or "", *edu.items]
    return "\n".join(parts)


# --- prompt ----------------------------------------------------------------------------


def _bullets(items: list[str], limit: int = 25) -> str:
    return "\n".join(f"- {i}" for i in items[:limit]) or "- (brak danych)"


def _offer_section(offer: Offer) -> str:
    lines = [f"Stanowisko: {offer.title}"]
    if offer.company:
        lines.append(f"Firma: {offer.company}")
    if offer.technologies:
        lines.append("Technologie: " + ", ".join(offer.technologies[:25]))
    lines.append("Wymagania:\n" + _bullets(offer.requirements))
    lines.append("Obowiązki:\n" + _bullets(offer.responsibilities))
    if offer.incomplete:
        lines.append("(Strona oferty się nie pobrała: znany jest tylko tytuł.)")
    return "\n".join(lines)


def _skills_section(profile: Profile) -> str:
    lines = []
    for group in profile.skill_groups:
        names = "; ".join(s.name for s in group.skills)
        lines.append(f"- {group.name}: {names}")
    return "\n".join(lines)


def _projects_section(candidates: list[Candidate]) -> str:
    blocks = []
    for c in candidates:
        p = c.project
        head = f"### {p.key}"
        if c.priority:
            head += f" (priorytet {c.priority} dla tej oferty"
            head += f"; akcent: {c.hint})" if c.hint else ")"
        lines = [head]
        if p.description:
            lines.append(f"Opis: {p.description}")
        if p.stack:
            lines.append("Stack (tagi wybieraj tylko stąd): " + ", ".join(p.stack))
        lines.append("Punkty do CV:\n" + _bullets(p.points))
        if p.rules:
            lines.append("Reguły projektu:\n" + _bullets(p.rules))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


PROMPT = """\
Przygotowujesz treść jednostronicowego CV pod konkretną ofertę pracy. Odpowiadasz
wyłącznie danymi w podanym schemacie JSON.

## Najważniejsza zasada
Tylko WYBIERASZ i PRZEFORMUŁOWUJESZ treść z profilu poniżej. Nie dopisujesz żadnych
faktów, technologii, liczb, firm, ról ani doświadczenia, których nie ma w profilu.
Wymagania oferty, których kandydat nie spełnia, po prostu pomijasz.

## Kandydat nie ma doświadczenia zawodowego w IT
Nie pisz nic, co sugeruje pracę zawodową lub komercyjną w IT: żadnych współpracowników,
klientów, pracodawców, firm, zespołów w pracy, „X lat doświadczenia”. Projekty to
projekty własne. Obecna praca kandydata (fizyczna, poza IT) nie trafia do CV.

## Język CV: {language_name}
{language_rules}

## Oferta
{offer}

## Profil: tekst bazowy podsumowania (wariant „{variant_name}”)
{variant_text}

## Profil: pula umiejętności (grupa: umiejętności oddzielone średnikami)
{skills}

## Profil: projekty do wyboru
{projects}

## Profil: wykształcenie (źródło dla bloku education)
{education}

## Twarde reguły z profilu (obowiązują zawsze)
{rules}

## Co zwrócić i limity
- title: tytuł zawodowy pod ofertę, najwyżej {t_title} znaków, zgodny z profilem
  (np. „Administrator systemów Linux”, „Junior DevOps Engineer”), bez słów „senior”,
  „ekspert”.
- summary: podsumowanie na bazie tekstu wariantu, dopasowane akcentami do oferty,
  najwyżej {t_summary} znaków, w pierwszej osobie bez zaimków („Buduję...”, „Utrzymuję...”).
- skill_groups: 2–5 grup o krótkich nazwach (do {t_group} znaków), razem najwyżej
  {t_skills} umiejętności, najpierw te z oferty. Każda umiejętność pochodzi z puli:
  nazwę możesz skrócić (np. wyciąć część nawiasu), ale nie wolno dodać nowej
  technologii. Dopiski „podstawy” / „teoria” zostają{qualifier_note}. Każda
  umiejętność najwyżej {t_skill} znaków.
- projects: dokładnie {n_projects} różne projekty z listy (pole key = nazwa po ###),
  w kolejności od najlepiej pasującego do oferty; preferuj projekty z priorytetem.
  Dla każdego 3–4 punkty (gdy projekt ma mniej punktów w profilu, możesz oprzeć
  punkt na jego opisie), każdy najwyżej {t_point} znaków, przeformułowane z punktów
  lub opisu projektu i dopasowane do oferty. tags: 2–4 pozycje ze stacku projektu,
  każda najwyżej {t_tag} znaków.
- education: jeden blok: school (nazwa szkoły), subtitle (profil/tytuł),
  items (najwyżej 4 krótkie punkty, np. kwalifikacje), wszystko z sekcji wykształcenia.
{feedback}"""

LANGUAGE_RULES = {
    "pl": "Całe CV po polsku.",
    "en": (
        "Oferta jest po angielsku, więc całe CV po angielsku: przetłumacz podsumowanie, "
        "punkty, nazwy grup umiejętności i wykształcenie. Nazwy technologii zostają "
        "bez zmian."
    ),
}


def english_rule(profile: Profile) -> str:
    """Poziom angielskiego z profilu i zakaz deklarowania płynności, jeśli profil go ma."""
    english = profile.english
    if english is None:
        return ""
    text = f"Poziom angielskiego to {english.level} (tak jak w profilu)."
    if english.no_fluency:
        text += " Nigdy „płynny”, „biegły”, „fluent”, „proficient”."
    return text


def build_prompt(
    profile: Profile,
    offer: Offer,
    *,
    language: CVLanguage,
    variant: SummaryVariant,
    candidates: list[Candidate],
    feedback: list[str] | None = None,
    previous: CVBlocks | None = None,
    base_text: str | None = None,
) -> str:
    feedback_text = ""
    if base_text and previous is None:
        feedback_text += (
            "\n## Gotowe CV kandydata jako baza (tekst z PDF)\n"
            "Zachowaj jego akcenty i dobór treści tam, gdzie pasują do oferty, ale każdy "
            "fakt i technologia muszą pochodzić z profilu powyżej.\n" + base_text + "\n"
        )
    if feedback:
        feedback_text += "\n## Poprawki do poprzedniej wersji (uwzględnij wszystkie)\n" + _bullets(
            feedback
        )
        if previous is not None:
            feedback_text += (
                "\n\nPoprzednia wersja (popraw tylko to, czego dotyczą uwagi; resztę zostaw):\n"
                + previous.model_dump_json(exclude={"languages"}, indent=1)
            )
    return PROMPT.format(
        language_name="polski" if language == "pl" else "angielski",
        language_rules=" ".join(filter(None, [LANGUAGE_RULES[language], english_rule(profile)])),
        offer=_offer_section(offer),
        variant_name=variant.name,
        variant_text=sanitize_source_text(variant.text),
        skills=_skills_section(profile),
        projects=_projects_section(candidates),
        education=_bullets(profile.education),
        rules=_bullets([r.text for r in profile.rules], limit=50),
        t_title=TARGET.title,
        t_summary=TARGET.summary,
        t_group=TARGET.skill_group_name,
        t_skills=TARGET.skills_total,
        t_skill=TARGET.skill,
        n_projects=min(TARGET.projects, len(candidates)),
        t_point=TARGET.point,
        t_tag=TARGET.tag,
        qualifier_note=(" (po angielsku „basics” / „theory”)" if language == "en" else ""),
        feedback=feedback_text,
    )


# --- schemat odpowiedzi ----------------------------------------------------------------


class _GeneratedBase(BaseModel):
    title: str = Field(max_length=HARD_MAX.title)
    summary: str = Field(max_length=HARD_MAX.summary)
    skill_groups: list[SkillGroupBlock] = Field(min_length=1, max_length=5)
    education: list[EducationBlock] = Field(min_length=1, max_length=2)


def response_model(project_keys: list[str]) -> type[_GeneratedBase]:
    """Schemat odpowiedzi z listą dozwolonych projektów (enum w JSON Schema)."""
    key_type = Literal.__getitem__(tuple(project_keys))  # type: ignore[attr-defined]
    project_model = create_model(
        "GeneratedProject",
        key=(key_type, ...),
        tags=(list[str], Field(default_factory=list, max_length=HARD_MAX.tags)),
        points=(
            list[str],
            Field(min_length=HARD_MAX.points_min, max_length=HARD_MAX.points_max),
        ),
    )
    return create_model(
        "GeneratedCV",
        __base__=_GeneratedBase,
        projects=(list[project_model], Field(min_length=1, max_length=HARD_MAX.projects)),
    )


# --- składanie bloków ------------------------------------------------------------------


def _short_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def project_link(project: Project) -> str | None:
    """Adres przy nazwie projektu: wersja produkcyjna, a gdy jej brak, repozytorium."""
    if project.production:
        return _short_url(project.production.split()[0])
    return _short_url(project.link) if project.link else None


def _base_tech(name: str) -> str:
    return normalize(re.sub(r"\s*\(.*?\)", "", name))


def filter_tags(tags: list[str], project: Project) -> list[str]:
    """Zostawia tylko tagi ze stacku projektu; gdy nic nie zostanie, bierze stack."""
    stack = {_base_tech(s): re.sub(r"\s*\(.*?\)", "", s).strip() for s in project.stack}
    out: list[str] = []
    for tag in tags:
        base = _base_tech(tag)
        match = next(
            (s for key, s in stack.items() if base and (base == key or key.startswith(base))),
            None,
        )
        if match and tag not in out and len(tag) <= HARD_MAX.tag:
            out.append(tag.strip())
    if not out:
        out = [s for s in stack.values() if len(s) <= HARD_MAX.tag][: TARGET.tags - 1]
    return out[: TARGET.tags]


def build_languages(profile: Profile, language: CVLanguage) -> list[LanguageBlock]:
    out = []
    for lang in profile.languages[:4]:
        name, level = lang.name.strip(), lang.level.strip()
        if language == "en":
            name = LANGUAGE_NAMES_EN.get(name.lower(), name)
            level = LEVELS_EN.get(level.lower(), level)
        out.append(LanguageBlock(name=name[:1].upper() + name[1:], level=level))
    return out


@dataclass
class GenerationResult:
    blocks: CVBlocks
    category: str | None
    variant: str
    project_keys: list[str]
    prompt: str
    warnings: list[str] = field(default_factory=list)


def generate_blocks(
    profile: Profile,
    offer: Offer,
    *,
    settings: Settings | None = None,
    feedback: list[str] | None = None,
    previous: CVBlocks | None = None,
    base_text: str | None = None,
) -> GenerationResult:
    """Generuje bloki CV pod ofertę.

    ``feedback`` to uwagi walidatora i pomiaru strony do poprawki, a ``previous``
    to poprawiana wersja (AI dostaje ją w prompcie). ``base_text`` to tekst gotowego
    CV bez źródła JSON (np. z PDF), na którym AI ma oprzeć nową wersję.
    """
    settings = settings or get_settings()
    language: CVLanguage = "en" if offer.language == "en" else "pl"
    category = offer_category(offer, settings)
    variant = pick_variant(profile, category)
    candidates = candidate_projects(profile, category)
    if not candidates:
        raise GeneratorError("Profil nie ma projektów z punktami do CV.")
    by_key = {c.project.key: c.project for c in candidates}

    prompt = build_prompt(
        profile,
        offer,
        language=language,
        variant=variant,
        candidates=candidates,
        feedback=feedback,
        previous=previous,
        base_text=base_text,
    )
    model = response_model(list(by_key))
    answer = run_claude(prompt, model, settings=settings)

    warnings: list[str] = []
    projects: list[ProjectBlock] = []
    seen: set[str] = set()
    for item in answer.projects:  # type: ignore[attr-defined]
        if item.key in seen:
            warnings.append(f"AI powtórzyło projekt {item.key}; pominięty.")
            continue
        seen.add(item.key)
        project = by_key[item.key]
        projects.append(
            ProjectBlock(
                name=project.display_name[: HARD_MAX.project_name],
                link=project_link(project),
                tags=filter_tags(item.tags, project),
                points=item.points,
            )
        )

    blocks = CVBlocks(
        language=language,
        title=answer.title,
        summary=answer.summary,
        skill_groups=answer.skill_groups,
        projects=projects,
        education=answer.education,
        languages=build_languages(profile, language),
    )
    claims = experience_claims(blocks_text(blocks))
    if claims:
        warnings.append(
            "Tekst sugeruje doświadczenie zawodowe (" + ", ".join(claims) + "); do poprawki."
        )
    return GenerationResult(
        blocks=blocks,
        category=category,
        variant=variant.name,
        project_keys=[p.name for p in projects],
        prompt=prompt,
        warnings=warnings,
    )


def cv_from_result(offer: Offer, result: GenerationResult, profile: Profile) -> CV:
    """Rekord CV (szkic) do zapisania w bazie."""
    from cv_app.render import TEMPLATE_VERSION

    return CV(
        offer_id=offer.id,
        language=result.blocks.language,
        status=CVStatus.DRAFT,
        blocks=result.blocks.model_dump(),
        variant=result.variant,
        profile_version=profile.version,
        template_version=TEMPLATE_VERSION,
    )
