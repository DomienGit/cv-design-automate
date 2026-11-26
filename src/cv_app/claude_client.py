"""Klient AI: Claude Code w trybie nieinteraktywnym (``claude -p``).

Cała komunikacja z modelem przechodzi przez :func:`run_claude`, żeby dało się ją
później podmienić na API Anthropic bez zmian w reszcie aplikacji.

Wywołanie:

- prompt idzie przez stdin (bez limitu długości argumentów),
- ``--json-schema`` wymusza odpowiedź w podanym schemacie (pole ``structured_output``),
- ``--tools ""`` wyłącza narzędzia: model tylko odpowiada, nic nie uruchamia,
- ``--no-session-persistence`` nie zapisuje sesji,
- katalog roboczy to pusty folder w ``data_dir``, żeby Claude Code nie wczytał
  CLAUDE.md z repozytorium ani plików profilu.

W testach używaj :class:`FakeClaude` (``use_runner``), nigdy prawdziwego ``claude``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, overload

from pydantic import BaseModel, ValidationError

from cv_app.config import Settings, get_settings


class ClaudeError(Exception):
    """Wywołanie Claude się nie udało (także po ponowieniach)."""


class ClaudeNotInstalled(ClaudeError):
    """Nie znaleziono komendy ``claude``."""


class ClaudeResponseError(ClaudeError):
    """Odpowiedź nie pasuje do schematu; warto ponowić."""


class Runner(Protocol):
    def __call__(self, prompt: str, schema: dict[str, Any], settings: Settings) -> Any: ...


# --- prawdziwe wywołanie -------------------------------------------------------------


def build_command(schema: dict[str, Any], settings: Settings) -> list[str]:
    cmd = [
        settings.claude_command,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False),
        "--tools",
        "",
        "--no-session-persistence",
    ]
    if settings.claude_model:
        cmd += ["--model", settings.claude_model]
    return cmd


def parse_output(stdout: str) -> Any:
    """Wyciąga ``structured_output`` z wyniku ``--output-format json``."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeResponseError(f"Claude zwrócił niepoprawny JSON: {stdout[:200]!r}") from exc
    if not isinstance(envelope, dict):
        raise ClaudeResponseError("Nieoczekiwany format odpowiedzi Claude.")
    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        detail = envelope.get("result") or envelope.get("subtype") or "nieznany błąd"
        raise ClaudeError(f"Claude zgłosił błąd: {str(detail)[:300]}")
    if "structured_output" in envelope and envelope["structured_output"] is not None:
        return envelope["structured_output"]
    # Zapas: starsze wersje oddają JSON tylko jako tekst w polu "result".
    result = envelope.get("result")
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise ClaudeResponseError("Odpowiedź Claude nie zawiera danych w schemacie.") from exc
    raise ClaudeResponseError("Odpowiedź Claude nie zawiera danych w schemacie.")


def _workdir(settings: Settings) -> Path:
    path = settings.data_dir / "claude-work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def subprocess_runner(prompt: str, schema: dict[str, Any], settings: Settings) -> Any:
    if shutil.which(settings.claude_command) is None:
        raise ClaudeNotInstalled(
            f"Nie znaleziono komendy {settings.claude_command!r}. Zainstaluj Claude Code "
            "i zaloguj się (claude, potem /login) albo ustaw CV_APP_CLAUDE_COMMAND."
        )
    try:
        proc = subprocess.run(
            build_command(schema, settings),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=settings.claude_timeout,
            cwd=_workdir(settings),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"Claude nie odpowiedział w {settings.claude_timeout} s.") from exc
    if proc.returncode != 0 and not proc.stdout.strip():
        raise ClaudeError(
            f"claude zakończył się kodem {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    return parse_output(proc.stdout)


# --- atrapa do testów ----------------------------------------------------------------


@dataclass
class FakeClaude:
    """Atrapa: zwraca kolejne odpowiedzi z listy i zapisuje prompty.

    Element listy może być wartością (zwracaną jako odpowiedź), wyjątkiem (rzucanym)
    albo funkcją ``(prompt, schema) -> odpowiedź``.
    """

    responses: list[Any] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, prompt: str, schema: dict[str, Any], settings: Settings) -> Any:
        self.calls.append((prompt, schema))
        if not self.responses:
            raise AssertionError("FakeClaude: brak przygotowanej odpowiedzi")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(prompt, schema)
        return item


_runner: Runner = subprocess_runner


@contextmanager
def use_runner(runner: Runner) -> Iterator[Runner]:
    """Tymczasowo podmienia sposób wywołania (np. na :class:`FakeClaude`)."""
    global _runner
    previous, _runner = _runner, runner
    try:
        yield runner
    finally:
        _runner = previous


# --- publiczne API -------------------------------------------------------------------


@overload
def run_claude[M: BaseModel](
    prompt: str, schema: type[M], *, settings: Settings | None = ..., sleep: Callable = ...
) -> M: ...
@overload
def run_claude(
    prompt: str,
    schema: dict[str, Any],
    *,
    settings: Settings | None = ...,
    sleep: Callable = ...,
) -> Any: ...
def run_claude(
    prompt: str,
    schema: type[BaseModel] | dict[str, Any],
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Wysyła prompt i zwraca odpowiedź zgodną ze schematem.

    ``schema`` to klasa Pydantic (wynik jest jej instancją, po walidacji) albo słownik
    JSON Schema (wynik to sparsowany JSON). Błędy przejściowe (limit czasu, zły format)
    są ponawiane ``claude_retries`` razy z rosnącą przerwą.
    """
    settings = settings or get_settings()
    model = schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None
    json_schema = model.model_json_schema() if model else schema

    attempts = settings.claude_retries + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            data = _runner(prompt, json_schema, settings)
            return model.model_validate(data) if model else data
        except ClaudeNotInstalled:
            raise
        except ValidationError as exc:
            last_error = ClaudeResponseError(f"Odpowiedź nie pasuje do schematu: {exc}")
        except ClaudeError as exc:
            last_error = exc
        if attempt < attempts:
            sleep(2.0 * attempt)
    raise ClaudeError(f"Claude: nieudane po {attempts} próbach. {last_error}") from last_error
