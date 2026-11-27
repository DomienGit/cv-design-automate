"""Pomiar dopasowania CV do jednej strony A4 (Playwright, Chromium).

Szablon ma stałą wysokość strony (``.page`` = 297 mm, ``overflow: hidden``), więc
nadmiar treści nie tworzy drugiej strony, tylko znika pod krawędzią. Dlatego mierzymy
w przeglądarce naturalną wysokość treści każdej kolumny (bez wolnego miejsca nad
stopką / językami) i porównujemy ją z wysokością strony. Przy okazji zbieramy
wysokości bloków (podsumowanie, projekty, umiejętności) i wolne miejsce, żeby pętla
poprawek wiedziała, co i o ile skrócić.

Jedno uruchomienie przeglądarki można wykorzystać do wielu pomiarów
(:class:`FitMeter`), bo pętla poprawek mierzy to samo CV kilka razy.
"""

from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Any

from pydantic import BaseModel, Field

from cv_app.config import Settings
from cv_app.render import RenderError, chromium_launch_args

PX_PER_MM = 96 / 25.4
# Tolerancja zaokrągleń przeglądarki: poniżej pół milimetra nie uznajemy przepełnienia.
TOLERANCE_MM = 0.5

_MEASURE_JS = """
() => {
  const box = (el) => el ? el.getBoundingClientRect() : null;
  const page = document.querySelector('.page');
  const limit = page ? page.clientHeight : window.innerHeight;
  const column = (sel, spacerSel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const height = Math.max(el.scrollHeight, box(el).height);
    const spacer = el.querySelector(spacerSel);
    let free = 0;
    if (spacer && spacer.previousElementSibling) {
      const prev = spacer.previousElementSibling;
      const mb = parseFloat(getComputedStyle(prev).marginBottom) || 0;
      free = box(spacer).top - box(prev).bottom - mb;
    }
    free = Math.max(free, 0);
    // Kolumny są elementami siatki: rozciągają się do wyższej z nich, a .page ucina
    // resztę. Naturalna wysokość treści = wysokość kolumny minus wolne miejsce.
    return {scroll: height - free, client: limit, free: Math.max(limit - (height - free), 0)};
  };
  const height = (sel) => {
    const el = document.querySelector(sel);
    return el ? box(el).height : 0;
  };
  const title = document.querySelector('.head .title');
  let titleLines = 0;
  if (title) {
    const lh = parseFloat(getComputedStyle(title).lineHeight) || 1;
    titleLines = Math.round(box(title).height / lh);
  }
  return {
    main: column('.main', '.clause'),
    side: column('.side', '.side-section.languages'),
    blocks: {
      summary: height('.block.summary'),
      projects: height('.block.projects'),
      education: height('.block.education'),
      skills: height('.side-section.skills'),
      contact: height('.side-section:not(.skills):not(.languages)'),
    },
    projects: Array.from(document.querySelectorAll('.project')).map(e => box(e).height),
    titleLines: titleLines,
  };
}
"""


class ColumnFit(BaseModel):
    overflow_mm: float  # ile treści wychodzi poza stronę (0, gdy się mieści)
    free_mm: float  # wolne miejsce nad stopką (0, gdy brak)

    @property
    def fits(self) -> bool:
        return self.overflow_mm <= TOLERANCE_MM


class FitReport(BaseModel):
    """Wynik pomiaru jednego CV."""

    main: ColumnFit  # prawa kolumna: tytuł, podsumowanie, projekty, edukacja
    side: ColumnFit  # lewy pasek: zdjęcie, kontakt, umiejętności, języki
    blocks_mm: dict[str, float] = Field(default_factory=dict)
    projects_mm: list[float] = Field(default_factory=list)
    title_lines: int = 1

    @property
    def fits(self) -> bool:
        return self.main.fits and self.side.fits and self.title_lines <= 1

    @property
    def overflow_mm(self) -> float:
        return max(self.main.overflow_mm, self.side.overflow_mm)

    def problems(self) -> list[str]:
        """Opis problemów po polsku: do panelu i do promptu skracającego."""
        out: list[str] = []
        if not self.main.fits:
            largest = max(range(len(self.projects_mm)), key=self.projects_mm.__getitem__, default=0)
            out.append(
                f"Prawa kolumna wychodzi poza stronę o {self.main.overflow_mm:.0f} mm: "
                "skróć podsumowanie albo punkty projektów "
                f"(najwyższy jest projekt nr {largest + 1})."
            )
        if not self.side.fits:
            out.append(
                f"Lewy pasek wychodzi poza stronę o {self.side.overflow_mm:.0f} mm: "
                "usuń kilka umiejętności albo skróć ich nazwy."
            )
        if self.title_lines > 1:
            out.append("Tytuł zawija się do drugiej linii: skróć go.")
        return out


def _mm(px: float) -> float:
    return round(px / PX_PER_MM, 1)


def report_from_measurement(data: dict[str, Any]) -> FitReport:
    """Zamienia surowy pomiar z przeglądarki (piksele) na :class:`FitReport`."""

    def column(raw: dict[str, float] | None) -> ColumnFit:
        if not raw:
            return ColumnFit(overflow_mm=0, free_mm=0)
        overflow = max(_mm(raw["scroll"] - raw["client"]), 0.0)
        return ColumnFit(overflow_mm=overflow, free_mm=_mm(raw.get("free", 0)))

    return FitReport(
        main=column(data.get("main")),
        side=column(data.get("side")),
        blocks_mm={k: _mm(v) for k, v in (data.get("blocks") or {}).items()},
        projects_mm=[_mm(v) for v in data.get("projects") or []],
        title_lines=int(data.get("titleLines") or 1),
    )


class FitMeter:
    """Przeglądarka otwarta na czas kilku pomiarów.

    >>> with FitMeter(settings) as meter:
    ...     report = meter.measure(html)
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    def __enter__(self) -> FitMeter:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(**chromium_launch_args(self.settings))
            self._page = self._browser.new_page(viewport={"width": 794, "height": 1123})
            self._page.emulate_media(media="print")
        except PlaywrightError as exc:
            self.close()
            raise RenderError(
                "Nie udało się uruchomić przeglądarki do pomiaru CV. Jeśli jej brakuje, "
                f"uruchom: uv run playwright install chromium. Szczegóły: {exc}"
            ) from exc
        return self

    def measure(self, html: str) -> FitReport:
        from playwright.sync_api import Error as PlaywrightError

        if self._page is None:
            raise RuntimeError("FitMeter: użyj w bloku with")
        try:
            self._page.set_content(html, wait_until="load")
            self._page.evaluate("document.fonts.ready")
            data = self._page.evaluate(_MEASURE_JS)
        except PlaywrightError as exc:
            raise RenderError(f"Pomiar CV się nie udał: {exc}") from exc
        return report_from_measurement(data)

    def close(self) -> None:
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                with contextlib.suppress(Exception):  # sprzątanie: błąd nie ma znaczenia
                    getattr(obj, method)()
        self._pw = self._browser = self._page = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def measure_fit(html: str, settings: Settings) -> FitReport:
    """Jednorazowy pomiar (otwiera i zamyka przeglądarkę)."""
    with FitMeter(settings) as meter:
        return meter.measure(html)
