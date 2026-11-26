"""Zadania w tle dla panelu (pobieranie ofert, podgląd CV): jedno naraz, log w pamięci."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any

from cv_app.pipeline import Log, PipelineError

JobFn = Callable[[Log], dict[str, Any] | None]


class JobBusy(Exception):
    """Inne zadanie jeszcze trwa."""


@dataclass
class Job:
    id: int
    kind: str
    label: str
    status: str = "running"  # running / done / error
    log: list[str] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "log": list(self.log),
            "error": self.error,
            "result": dict(self.result),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class JobRunner:
    """Uruchamia jedno zadanie naraz w wątku. ``background=False`` (testy) liczy od razu."""

    def __init__(self, *, background: bool = True) -> None:
        self.background = background
        self._lock = threading.Lock()
        self._ids = count(1)
        self._jobs: dict[int, Job] = {}
        self.current: Job | None = None

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, kind: str, label: str, fn: JobFn) -> Job:
        with self._lock:
            if self.current is not None and self.current.status == "running":
                raise JobBusy(f"Trwa już: {self.current.label}")
            job = Job(id=next(self._ids), kind=kind, label=label)
            self._jobs[job.id] = job
            self.current = job
        if self.background:
            threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        else:
            self._run(job, fn)
        return job

    def _run(self, job: Job, fn: JobFn) -> None:
        try:
            job.result = fn(job.log.append) or {}
            job.status = "done"
        except PipelineError as exc:
            job.error = str(exc)
            job.status = "error"
        except Exception as exc:  # nieprzewidziany błąd: pokaż w panelu, nie zabijaj serwera
            traceback.print_exc()
            job.error = f"Nieoczekiwany błąd: {exc}"
            job.status = "error"
        finally:
            job.finished_at = datetime.now(UTC)
