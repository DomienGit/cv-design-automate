"""Walidator reguł treści CV: bloki CV + profil → lista naruszeń.

Sprawdza twarde reguły z calloutów ``[!important]`` w profilu (i limity z szablonu).
Gdzie się da, reguła jest czytana z profilu (np. które technologie są zakazane, które
projekty wymagają podania AI-asysty), a nie wpisana na sztywno w kod.

Komunikaty są po polsku i trafiają do dwóch miejsc: do promptu poprawiającego CV
(pętla poprawek w :mod:`cv_app.pipeline`) i do ostrzeżeń w panelu.

Część reguł (np. „bez dat przy projektach”, „bez liczby użytkowników”) sprawdzamy
po słowach kluczowych, więc to heurystyka: może coś przepuścić albo fałszywie zgłosić.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cv_app.cv_blocks import CVBlocks, ProjectBlock
from cv_app.generator import blocks_text, experience_claims
from cv_app.profile import QUALIFIER_WORDS, UNCONFIRMED_MARKERS, Profile, Project
from cv_app.relevance import normalize


@dataclass(frozen=True)
class Violation:
    rule: str  # krótki identyfikator, np. "forbidden-tech"
    message: str  # opis dla AI i dla panelu

    def __str__(self) -> str:
        return self.message


# --- pomocnicze ------------------------------------------------------------------------

# „Azure: brak doświadczenia, nigdy nie wpisywać do CV” / „Azure: nigdy nie wpisywać do CV”
_FORBIDDEN_TECH = re.compile(
    r"([A-Z][\w.+#/-]*)\s*:\s*(?:brak doświadczenia[,.]?\s*)?nigdy nie wpisywać do CV",
    re.IGNORECASE,
)
# Dopiski przy umiejętności w obu językach CV.
_QUALIFIER_ALIASES = {"podstawy": ("podstawy", "basics", "basic"), "teoria": ("teoria", "theory")}
_ALL_QUALIFIERS = tuple(w for ws in _QUALIFIER_ALIASES.values() for w in ws) + QUALIFIER_WORDS

# Zakazane określenia poziomu angielskiego (sprawdzane w zdaniach o języku).
_FLUENCY = re.compile(r"płynn\w*|biegł\w*|biegle|fluent\w*|fluency|proficien\w*", re.I)
_LANGUAGE_CONTEXT = re.compile(r"angiel|english|\b[ABC][12]\b", re.I)

# AI-asysta podana wprost.
_AI_ASSIST = re.compile(
    r"narzędzi\w*\s+AI|AI[- ]?asyst\w*|asyst\w*\s+AI|(?:pomoc|wsparci|użyci|wykorzystani)\w*"
    r"\s+(?:narzędzi\w*\s+)?AI|Claude(?:\s+Code)?|AI[- ]assist\w*|AI tools|with AI\b|"
    r"using AI\b|AI-powered development|LLM",
    re.I,
)
_MANUAL_CODE = re.compile(r"ręczn\w*|samodzielnie napisa\w*|by hand|hand-?written|manually", re.I)

# „Odtworzyłem na podstawie gotowego projektu”.
_REBUILT = re.compile(r"odtworzy\w*|na podstawie|wzorowan\w*|recreat\w*|replicat\w*|based on", re.I)

# Daty przy projektach: lata i miesiące.
_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?![\d.])")
_MONTHS = re.compile(
    r"\b(?:stycz\w*|lut\w*|marc\w*|kwie\w*|maj[au]?|czerw\w*|lip\w*|sierp\w*|wrze[sś]\w*|"
    r"październik\w*|paździer\w*|listopad\w*|grud\w*|january|february|march|april|june|"
    r"july|august|september|october|november|december)\b",
    re.I,
)

# Tryb pracy, lokalizacja, forma umowy: weryfikowane przy wyborze oferty, nie w CV.
_WORK_TERMS = re.compile(
    r"zdaln\w*|hybryd\w*|stacjonarn\w*|\bremote\b|\bhybrid\b|on-?site|\bB2B\b|"
    r"umow\w* o pracę|\bUoP\b|relokac\w*|relocat\w*|pełny etat|full[- ]time|part[- ]time",
    re.I,
)

# Liczby użytkowników i ruchu (gdy projekt ma regułę „nie pisać o użytkownikach”).
_USAGE = re.compile(
    r"użytkownik\w*|\busers?\b|\bruch\w*|traffic|odwiedz\w*|\bvisits?\b|wyświetle\w*|"
    r"\bviews?\b|\d[\d\s]*\+?\s*(?:osób|people|uczestnik\w*)",
    re.I,
)


def _base(name: str) -> str:
    """„podstawy Ansible (teoria)” → „ansible”; „AWS (EC2, VPC)” → „aws”."""
    text = re.sub(r"\(.*?\)", " ", name)
    words = [w for w in normalize(text).split() if w not in _ALL_QUALIFIERS]
    return " ".join(words).strip(" ,;:-")


def _has_qualifier(name: str, qualifier: str) -> bool:
    low = normalize(name)
    return any(re.search(rf"\b{w}\b", low) for w in _QUALIFIER_ALIASES.get(qualifier, ()))


def forbidden_technologies(profile: Profile) -> list[str]:
    """Technologie, których nigdy nie wpisujemy do CV (z reguł profilu, np. Azure)."""
    found: list[str] = []
    for rule in profile.rules:
        for m in _FORBIDDEN_TECH.finditer(rule.text):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


def _aliases(name: str) -> list[str]:
    """Nazwy, pod którymi umiejętność może wystąpić w CV.

    „Bash/Shell” → bash/shell, bash, shell; „React + Vite” → react, vite;
    „Nginx (reverse proxy, SSL/TLS)” → nginx, reverse proxy, ssl/tls, ssl, tls.
    """
    out = [_base(name)]
    inner = re.findall(r"\((.*?)\)", name)
    parts = [p for chunk in inner for p in chunk.split(",")]
    for part in [re.sub(r"\(.*?\)", " ", name), *parts]:
        base = _base(part)
        out.append(base)
        out += [_base(p) for p in re.split(r"\s*(?:/|\+|&|\bi\b|\band\b)\s*", base)]
    return [a for a in dict.fromkeys(out) if a]


def _profile_skills(profile: Profile) -> dict[str, list[str]]:
    """Nazwa (i jej warianty) → dopiski z profilu; pula umiejętności i stacki projektów."""
    out: dict[str, list[str]] = {}
    for skill in profile.all_skills:
        main, *rest = _aliases(skill.name)
        out.setdefault(main, []).extend(skill.qualifiers)
        for alias in rest:
            out.setdefault(alias, [])
    for project in profile.projects:
        for tech in project.stack:
            for alias in _aliases(tech):
                out.setdefault(alias, [])
    return out


def _profile_corpus(profile: Profile) -> str:
    """Tekst profilu, w którym może paść nazwa technologii (bez treści reguł)."""
    parts = [v.text for v in profile.variants]
    for project in profile.projects:
        parts += [project.description, *project.points, *project.stack]
    return " " + " ".join(normalize(x) for x in parts) + " "


def _mentioned(base: str, corpus: str) -> bool:
    """Nazwa pada w opisie profilu jako osobne słowo/fraza (np. „CI/CD” w wariancie)."""
    return bool(base) and re.search(rf"(?<![\w]){re.escape(base)}(?![\w])", corpus) is not None


def _find_skill(base: str, known: dict[str, list[str]]) -> str | None:
    if not base:
        return None
    if base in known:
        return base
    # Skrócona nazwa („Docker” z „Docker Compose”) albo nazwa z dopiskiem („AWS EC2”).
    for key in known:
        if key.startswith(base + " ") or base.startswith(key + " "):
            return key
    return None


def _looks_technical(skill: str) -> bool:
    """W CV po angielsku przetłumaczone opisy („Log analysis”) nie są nazwami technologii.

    Za nazwę technologii uznajemy jedno słowo, słowo z wielką literą poza pierwszym
    albo znaki typowe dla nazw (cyfry, ``.``, ``+``, ``#``, ``/``).
    """
    words = re.sub(r"\(.*?\)", " ", skill).split()
    if len(words) <= 1 or re.search(r"[\d.+#/]", skill):
        return True
    return any(w[:1].isupper() for w in words[1:])


def _project_text(block: ProjectBlock) -> str:
    return "\n".join([block.name, *block.points])


def _rules_text(project: Project) -> str:
    return " ".join(project.rules).lower()


# --- reguły ----------------------------------------------------------------------------


def check_forbidden_tech(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    text = blocks_text(blocks) + "\n" + _skills_and_tags(blocks)
    out = []
    for tech in forbidden_technologies(profile):
        if re.search(rf"(?<![\w]){re.escape(tech)}(?![\w])", text, re.I):
            out.append(
                Violation(
                    "forbidden-tech",
                    f"Usuń „{tech}” z całego CV: kandydat nie ma z tym doświadczenia "
                    "(reguła profilu: nigdy w CV).",
                )
            )
    return out


def _skills_and_tags(blocks: CVBlocks) -> str:
    parts = [s for g in blocks.skill_groups for s in g.skills]
    parts += [t for p in blocks.projects for t in p.tags]
    return "\n".join(parts)


def check_english_level(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    """Poziom angielskiego jak w profilu; bez „płynny” / „fluent”, gdy profil tego zabrania."""
    english = profile.english
    if english is None:
        return []
    out = []
    if english.no_fluency:
        sentences = re.split(r"(?<=[.!?;])\s+|\n", blocks_text(blocks))
        for sentence in sentences:
            if _LANGUAGE_CONTEXT.search(sentence) and (m := _FLUENCY.search(sentence)):
                out.append(
                    Violation(
                        "english-level",
                        f"Nie pisz „{m.group(0)}” o angielskim: poziom to {english.level}, "
                        "bez deklarowania płynności.",
                    )
                )
    for lang in blocks.languages:
        if lang.name.lower() not in ("angielski", "english"):
            continue
        wrong_level = english.level.lower() not in lang.level.lower()
        if wrong_level or (english.no_fluency and _FLUENCY.search(lang.level)):
            out.append(
                Violation(
                    "english-level",
                    f"Poziom angielskiego w CV ma być „{english.level}”, a jest „{lang.level}”.",
                )
            )
    return out


def check_skills(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    known = _profile_skills(profile)
    corpus = _profile_corpus(profile)
    forbidden = {normalize(t) for t in forbidden_technologies(profile)}
    out = []
    for group in blocks.skill_groups:
        for skill in group.skills:
            if _base(skill) in forbidden:
                continue  # zgłasza check_forbidden_tech
            key = _find_skill(_base(skill), known)
            if key is None:
                if _mentioned(_base(skill), corpus):
                    continue  # pada w opisie profilu (np. „CI/CD” w podsumowaniu)
                if blocks.language == "en" and not _looks_technical(skill):
                    continue  # przetłumaczony opis umiejętności z profilu
                out.append(
                    Violation(
                        "unknown-skill",
                        f"Umiejętności „{skill}” nie ma w profilu: usuń ją albo użyj nazwy "
                        "z puli umiejętności.",
                    )
                )
                continue
            missing = [q for q in dict.fromkeys(known[key]) if not _has_qualifier(skill, q)]
            if missing:
                word = missing[0] if blocks.language == "pl" else _QUALIFIER_ALIASES[missing[0]][1]
                out.append(
                    Violation(
                        "skill-qualifier",
                        f"Umiejętność „{skill}” musi mieć dopisek „{word}”, jak w profilu.",
                    )
                )
    return out


def check_projects(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    out = []
    for block in blocks.projects:
        project = profile.project(block.name)
        if project is None:
            out.append(Violation("unknown-project", f"Projektu „{block.name}” nie ma w profilu."))
            continue
        if not project.standalone or project.merged_into:
            target = project.merged_into or "innego projektu"
            out.append(
                Violation(
                    "merged-project",
                    f"„{block.name}” nie może być osobnym projektem w CV "
                    f"(reguła profilu: część {target}).",
                )
            )
        text = _project_text(block)
        if project.ai_assisted:
            if not _AI_ASSIST.search(text):
                out.append(
                    Violation(
                        "ai-assisted",
                        f"W projekcie „{block.name}” napisz wprost, że powstał z użyciem "
                        "narzędzi AI (reguła profilu).",
                    )
                )
            if m := _MANUAL_CODE.search(text):
                out.append(
                    Violation(
                        "ai-assisted",
                        f"W projekcie „{block.name}” nie pisz „{m.group(0)}”: kod powstał "
                        "z pomocą AI.",
                    )
                )
        rules = _rules_text(project)
        if "odtworzy" in rules and not _REBUILT.search(text):
            out.append(
                Violation(
                    "rebuilt-project",
                    f"W projekcie „{block.name}” napisz wprost, że to odtworzenie gotowego "
                    "projektu (np. „na podstawie gotowego projektu odtworzyłem…”).",
                )
            )
        if "użytkownik" in rules and "nie pisać" in rules and (m := _USAGE.search(text)):
            out.append(
                Violation(
                    "usage-numbers",
                    f"W projekcie „{block.name}” nie pisz o użytkownikach ani ruchu "
                    f"(znalezione: „{m.group(0).strip()}”).",
                )
            )
        if m := (_YEAR.search(text) or _MONTHS.search(text)):
            out.append(
                Violation(
                    "project-dates",
                    f"Usuń datę z projektu „{block.name}” („{m.group(0)}”): w CV nie "
                    "pokazujemy dat przy projektach.",
                )
            )
    return out


def check_experience(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    claims = experience_claims(blocks_text(blocks))
    if not claims:
        return []
    return [
        Violation(
            "experience",
            "Usuń sformułowania sugerujące pracę zawodową w IT ("
            + ", ".join(f"„{c}”" for c in claims)
            + "): kandydat nie ma doświadczenia komercyjnego.",
        )
    ]


def check_work_terms(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    text = "\n".join([blocks.title, blocks.summary])
    if m := _WORK_TERMS.search(text):
        return [
            Violation(
                "work-terms",
                f"Usuń „{m.group(0)}”: trybu pracy, lokalizacji i formy umowy nie wpisujemy do CV.",
            )
        ]
    return []


def check_markers(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    text = (blocks_text(blocks) + "\n" + _skills_and_tags(blocks)).upper()
    return [
        Violation("unconfirmed", f"Usuń treść oznaczoną „{marker}”: to niepotwierdzone dane.")
        for marker in UNCONFIRMED_MARKERS
        if marker in text
    ]


CHECKS = (
    check_forbidden_tech,
    check_english_level,
    check_skills,
    check_projects,
    check_experience,
    check_work_terms,
    check_markers,
)


def validate(blocks: CVBlocks, profile: Profile) -> list[Violation]:
    """Wszystkie naruszenia reguł w CV (pusta lista = CV zgodne z profilem)."""
    out: list[Violation] = []
    for check in CHECKS:
        for violation in check(blocks, profile):
            if violation not in out:
                out.append(violation)
    return out


# --- gotowe CV bez źródła (tylko tekst z PDF) ----------------------------------------


def known_technologies(profile: Profile) -> list[str]:
    """Znormalizowane nazwy technologii z profilu (pula umiejętności i stacki projektów)."""
    return list(_profile_skills(profile))


def validate_text(text: str, profile: Profile) -> list[Violation]:
    """Reguły, które da się sprawdzić na samym tekście CV (np. z PDF bez źródła JSON).

    Sprawdza zakazane technologie, deklarację płynności angielskiego, pracę zawodową
    i znaczniki „DO UZUPEŁNIENIA”. Pełne sprawdzenie (umiejętności, projekty) wymaga
    bloków CV, więc takie CV przy edycji przechodzi jeszcze przez :func:`validate`.
    """
    out: list[Violation] = []
    for tech in forbidden_technologies(profile):
        if re.search(rf"(?<![\w]){re.escape(tech)}(?![\w])", text, re.I):
            out.append(Violation("forbidden-tech", f"Usuń „{tech}” z całego CV (reguła profilu)."))
    english = profile.english
    if english is not None and english.no_fluency:
        for sentence in re.split(r"(?<=[.!?;])\s+|\n", text):
            if _LANGUAGE_CONTEXT.search(sentence) and (m := _FLUENCY.search(sentence)):
                out.append(
                    Violation(
                        "english-level",
                        f"Nie pisz „{m.group(0)}” o angielskim: poziom to {english.level}.",
                    )
                )
                break
    claims = experience_claims(text)
    if claims:
        out.append(
            Violation(
                "experience",
                "Usuń sformułowania sugerujące pracę zawodową w IT ("
                + ", ".join(f"„{c}”" for c in claims)
                + ").",
            )
        )
    upper = text.upper()
    out += [
        Violation("unconfirmed", f"Usuń treść oznaczoną „{marker}”.")
        for marker in UNCONFIRMED_MARKERS
        if marker in upper
    ]
    return out
