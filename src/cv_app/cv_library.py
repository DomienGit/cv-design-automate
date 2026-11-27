"""Biblioteka gotowych CV i dopasowanie ich do oferty.

Dominik trzyma gotowe CV w folderze ``CV_APP_CV_LIBRARY_DIR`` (domyślnie ``./cv``),
w podfolderach per stanowisko, np. ``cv/DevOps/``, ``cv/Helpdesk/``. Plik CV to:

- ``nazwa.json``: bloki CV (jak w :mod:`cv_app.cv_blocks`), czyli źródło, które da
  się edytować i wyrenderować od nowa; najlepszy format,
- ``nazwa.md`` / ``nazwa.txt``: tekst CV,
- ``nazwa.pdf``: sam PDF; tekst wyciągamy skryptem (``pypdf``).

Pliki o tej samej nazwie w jednym folderze to jedno CV (np. ``cv-aws.pdf`` +
``cv-aws.json``); źródło wygrywa nad PDF przy odczycie tekstu.

Dopasowanie jest deterministyczne (bez AI): z oferty bierzemy listę technologii
(tagi z portalu + technologie z profilu, które padają w tytule, wymaganiach
i obowiązkach), w każdym CV szukamy tych samych nazw i liczymy pokrycie w procentach.
Podfolder o kategorii oferty (z filtra trafności) dostaje premię. AI decyduje potem
tylko na podstawie kilku najlepszych CV (:mod:`cv_app.cv_match`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from cv_app.cv_blocks import CVBlocks
from cv_app.generator import CATEGORY_KEYWORDS, blocks_text
from cv_app.models import Offer
from cv_app.relevance import CATEGORIES, _contains, normalize

SOURCE_EXTS = (".json", ".md", ".txt")
LIBRARY_EXTS = (*SOURCE_EXTS, ".pdf")
CATEGORY_BONUS = 10  # punkty za podfolder pasujący do kategorii oferty

Action = Literal["use", "edit", "new"]
ACTION_LABELS: dict[str, str] = {
    "use": "gotowe CV bez zmian",
    "edit": "edycja gotowego CV",
    "new": "nowe CV",
}

# Klauzula RODO w stopce: nie jest treścią CV (i ma słowa typu „pracodawca”).
_CLAUSE = re.compile(
    r"(wyrażam zgodę|wyrazam zgode|zgodnie z art\. 6|i hereby (?:agree|consent)|"
    r"i agree to the processing).*",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d \-()]{7,}\d(?!\w)")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:pl|com|dev|io|net|org)(?:/\S*)?", re.I)
_POLISH = re.compile(r"[ąćęłńóśźż]", re.I)
_EN_WORDS = re.compile(r"\b(?:the|and|with|of|in|for|projects?|skills|education)\b", re.I)


class LibraryError(Exception):
    """Nie udało się odczytać pliku CV z biblioteki."""


@dataclass
class LibraryCV:
    key: str  # ścieżka względna bez rozszerzenia, np. "DevOps/cv-aws"
    folder: str  # pierwszy podfolder, np. "DevOps" ("" dla plików w głównym folderze)
    category: str | None  # kategoria oferty, na którą wskazuje podfolder
    text: str  # tekst CV bez klauzuli RODO
    pdf: Path | None = None
    source: Path | None = None  # .json / .md / .txt
    blocks: CVBlocks | None = None  # gdy źródłem jest JSON z blokami CV

    @property
    def language(self) -> str:
        if self.blocks is not None:
            return self.blocks.language
        return guess_language(self.text)

    @property
    def files(self) -> list[str]:
        return [p.name for p in (self.source, self.pdf) if p is not None]


def guess_language(text: str) -> str:
    """„pl” albo „en” po polskich znakach i częstych angielskich słowach."""
    polish = len(_POLISH.findall(text))
    english = len(_EN_WORDS.findall(text))
    return "en" if english > 2 * polish + 3 else "pl"


def folder_category(name: str) -> str | None:
    """Kategoria oferty dla podfolderu („Administrator Linux” → admin, „DevOps” → devops)."""
    text = normalize(name.replace("_", " ").replace("-", " "))
    if not text:
        return None
    for category, keywords in CATEGORIES.items():
        if any(_contains(text, k) for k in keywords):
            return category
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(_contains(text, k) for k in keywords):
            return category
    return None


def strip_clause(text: str) -> str:
    return _CLAUSE.sub("", text).strip()


def anonymize(text: str, private: list[str] | None = None) -> str:
    """Tekst CV do promptu: bez e-maili, telefonów, adresów stron i danych kontaktowych."""
    for value in sorted(filter(None, private or []), key=len, reverse=True):
        text = re.sub(re.escape(value), " ", text, flags=re.IGNORECASE)
    text = _EMAIL.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _PHONE.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, OSError, ValueError) as exc:
        raise LibraryError(f"{path.name}: nie udało się odczytać PDF ({exc})") from exc


def blocks_full_text(blocks: CVBlocks) -> str:
    skills = [s for g in blocks.skill_groups for s in g.skills]
    tags = [t for p in blocks.projects for t in p.tags]
    names = [p.name for p in blocks.projects]
    langs = [f"{lang.name} {lang.level}" for lang in blocks.languages]
    return "\n".join([blocks_text(blocks), *names, *skills, *tags, *langs])


def _stem(path: Path) -> str:
    name = path.name[: -len(path.suffix)]
    return name.removesuffix(".blocks")


def _read_source(path: Path) -> tuple[str, CVBlocks | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LibraryError(f"{path.name}: nie udało się odczytać ({exc})") from exc
    if path.suffix.lower() != ".json":
        return raw, None
    try:
        blocks = CVBlocks.model_validate_json(raw)
    except ValidationError as exc:
        raise LibraryError(f"{path.name}: to nie są bloki CV ({exc.error_count()} błędów)") from exc
    return blocks_full_text(blocks), blocks


def scan_library(root: Path) -> tuple[list[LibraryCV], list[str]]:
    """Wszystkie CV z biblioteki i lista ostrzeżeń (pliki, których nie dało się odczytać)."""
    if not root.is_dir():
        return [], []
    groups: dict[tuple[Path, str], list[Path]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (
            not path.is_file()
            or path.suffix.lower() not in LIBRARY_EXTS
            or any(part.startswith(".") for part in rel.parts)
        ):
            continue
        groups.setdefault((path.parent, _stem(path)), []).append(path)

    out: list[LibraryCV] = []
    warnings: list[str] = []
    for (parent, stem), paths in groups.items():
        rel_parent = parent.relative_to(root)
        folder = rel_parent.parts[0] if rel_parent.parts else ""
        pdf = next((p for p in paths if p.suffix.lower() == ".pdf"), None)
        sources = sorted(
            (p for p in paths if p.suffix.lower() in SOURCE_EXTS),
            key=lambda p: SOURCE_EXTS.index(p.suffix.lower()),
        )
        text, blocks, source = "", None, None
        for candidate in sources:
            try:
                text, blocks = _read_source(candidate)
            except LibraryError as exc:
                warnings.append(str(exc))
                continue
            source = candidate
            break
        if source is None and pdf is not None:
            try:
                text = pdf_text(pdf)
            except LibraryError as exc:
                warnings.append(str(exc))
                continue
        text = strip_clause(text)
        if not text.strip():
            if pdf is not None and source is None:
                warnings.append(f"{pdf.name}: PDF bez tekstu (skan?), pominięty")
            continue
        out.append(
            LibraryCV(
                key=(rel_parent / stem).as_posix(),
                folder=folder,
                category=folder_category(folder) if folder else None,
                text=text,
                pdf=pdf,
                source=source,
                blocks=blocks,
            )
        )
    return out, warnings


# --- dopasowanie -----------------------------------------------------------------------


def _term(name: str) -> str:
    """„AWS (EC2, VPC)” → „aws”; „podstawy Ansible” zostaje jak jest (bez nawiasu)."""
    return normalize(re.sub(r"\(.*?\)", " ", name)).strip(" ,;:-.")


def _found(term: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None


def offer_text(offer: Offer) -> str:
    parts = [offer.title, *offer.requirements, *offer.responsibilities, *offer.technologies]
    return " " + normalize("\n".join(parts)) + " "


def offer_terms(
    offer: Offer, vocabulary: list[str], *, exclude: list[str] | None = None
) -> list[str]:
    """Technologie z oferty: tagi z portalu + nazwy z profilu, które padają w ofercie.

    ``exclude`` to technologie, których nigdy nie ma w CV (np. Azure z reguł profilu):
    nie liczą się do pokrycia, bo żadne CV nie może ich mieć.
    """
    skip = {_term(x) for x in exclude or []}
    text = offer_text(offer)
    terms: list[str] = []
    for name in offer.technologies:
        term = _term(name)
        if len(term) >= 2 and term not in skip:
            terms.append(term)
    for term in vocabulary:
        if len(term) >= 2 and term not in skip and _found(term, text):
            terms.append(term)
    # Bez duplikatów i bez krótszych nazw zawartych w dłuższych („docker” przy
    # „docker compose” zostaje, bo to osobne słowo, ale tylko raz).
    return list(dict.fromkeys(terms))


def _title_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w+#]{3,}", normalize(text))}


@dataclass
class Candidate:
    cv: LibraryCV
    score: int  # 0–100: pokrycie technologii + premia za podfolder
    coverage: int  # sam procent pokrycia technologii z oferty
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    category_match: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "key": self.cv.key,
            "folder": self.cv.folder,
            "score": self.score,
            "coverage": self.coverage,
            "matched": self.matched,
            "missing": self.missing,
            "category_match": self.category_match,
            "has_source": self.cv.blocks is not None,
            "language": self.cv.language,
            "files": self.cv.files,
        }


def score_cv(cv: LibraryCV, terms: list[str], offer: Offer, category: str | None) -> Candidate:
    text = " " + normalize(cv.text) + " "
    if terms:
        matched = [t for t in terms if _found(t, text)]
        missing = [t for t in terms if t not in matched]
        coverage = round(100 * len(matched) / len(terms))
    else:
        # Oferta bez technologii (np. niepełna): podobieństwo słów tytułu.
        words = _title_words(offer.title)
        matched = sorted(w for w in words if _found(w, text))
        missing = sorted(words - set(matched))
        coverage = round(100 * len(matched) / len(words)) if words else 0
    category_match = bool(category) and cv.category == category
    score = min(100, coverage + (CATEGORY_BONUS if category_match else 0))
    return Candidate(cv, score, coverage, matched, missing, category_match)


def rank(
    cvs: list[LibraryCV],
    offer: Offer,
    *,
    vocabulary: list[str],
    category: str | None,
    exclude: list[str] | None = None,
) -> list[Candidate]:
    """CV od najlepiej pasującego; przy remisie najpierw podfolder kategorii i CV ze źródłem."""
    terms = offer_terms(offer, vocabulary, exclude=exclude)
    scored = [score_cv(cv, terms, offer, category) for cv in cvs]
    return sorted(
        scored,
        key=lambda c: (-c.score, not c.category_match, c.cv.blocks is None, c.cv.key),
    )


def decide_by_score(score: int, *, use_threshold: int, edit_threshold: int) -> Action:
    if score >= use_threshold:
        return "use"
    if score >= edit_threshold:
        return "edit"
    return "new"
