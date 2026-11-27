"""Aplikacja FastAPI: panel (oferty, podgląd CV) i API zadań."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from cv_app import __version__
from cv_app.config import Settings, get_settings
from cv_app.db import init_db, make_engine
from cv_app.jobs import Job, JobBusy, JobFn, JobRunner
from cv_app.models import CV, Offer, OfferStatus
from cv_app.pipeline import (
    Log,
    fetch_offers,
    preview_cv,
    preview_path,
    read_preview_report,
    restore_offer,
)
from cv_app.portals import source_label

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(settings: Settings | None = None, *, jobs: JobRunner | None = None) -> FastAPI:
    settings = settings or get_settings()
    jobs = jobs or JobRunner()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(engine)
        yield
        engine.dispose()

    app = FastAPI(title="CV app", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.jobs = jobs
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    def offer_view(offer: Offer) -> dict[str, object]:
        data = offer.model_dump(mode="json")
        data["has_preview"] = preview_path(settings, offer.id or 0).is_file()
        report = read_preview_report(settings, offer.id or 0) if data["has_preview"] else None
        data["preview_problems"] = report.problems if report else []
        data["source_label"] = source_label(offer.source)
        return data

    def list_offers(session: Session) -> dict[str, list[dict[str, object]]]:
        offers = session.exec(select(Offer).order_by(Offer.created_at.desc(), Offer.id.desc()))
        groups: dict[str, list[dict[str, object]]] = {"offers": [], "rejected": []}
        for offer in offers:
            if offer.status == OfferStatus.REJECTED:
                groups["rejected"].append(offer_view(offer))
            elif offer.status != OfferStatus.HIDDEN:
                groups["offers"].append(offer_view(offer))
        return groups

    def start_job(kind: str, label: str, fn: JobFn) -> dict[str, object]:
        try:
            job = jobs.start(kind, label, fn)
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.view()

    def get_offer(session: Session, offer_id: int) -> Offer:
        offer = session.get(Offer, offer_id)
        if offer is None:
            raise HTTPException(status_code=404, detail=f"Nie ma oferty #{offer_id}.")
        return offer

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: Annotated[Session, Depends(get_session)]) -> HTMLResponse:
        current: Job | None = jobs.current
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "settings": settings.public_view(),
                "version": __version__,
                "job": current.view() if current else None,
                **list_offers(session),
            },
        )

    @app.get("/api/offers")
    def api_offers(session: Annotated[Session, Depends(get_session)]) -> dict[str, object]:
        return list_offers(session)

    @app.post("/api/offers/fetch")
    def api_fetch_offers() -> dict[str, object]:
        def run(log: Log) -> dict[str, object]:
            result = fetch_offers(settings, engine, log)
            return {"added": len(result.added), "rejected": len(result.rejected)}

        return start_job("fetch", "Pobieranie ofert z Gmaila", run)

    @app.post("/api/offers/{offer_id}/restore")
    def api_restore(
        offer_id: int, session: Annotated[Session, Depends(get_session)]
    ) -> dict[str, object]:
        offer = get_offer(session, offer_id)
        return start_job(
            "restore",
            f"Przywracanie: {offer.title}",
            lambda log: restore_offer(settings, engine, offer_id, log),
        )

    @app.post("/api/offers/{offer_id}/cv-preview")
    def api_cv_preview(
        offer_id: int, session: Annotated[Session, Depends(get_session)]
    ) -> dict[str, object]:
        offer = get_offer(session, offer_id)

        def run(log: Log) -> dict[str, object]:
            preview_cv(settings, engine, offer_id, log)
            report = read_preview_report(settings, offer_id)
            return {
                "offer_id": offer_id,
                "pdf_url": f"/offers/{offer_id}/cv-preview.pdf",
                "problems": report.problems if report else [],
            }

        return start_job("cv-preview", f"Podgląd CV: {offer.title}", run)

    @app.get("/offers/{offer_id}/cv-preview.pdf")
    def cv_preview_pdf(offer_id: int) -> FileResponse:
        path = preview_path(settings, offer_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Brak podglądu CV dla tej oferty.")
        return FileResponse(path, media_type="application/pdf", content_disposition_type="inline")

    @app.get("/api/jobs/current")
    def api_current_job() -> dict[str, object] | None:
        return jobs.current.view() if jobs.current else None

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int) -> dict[str, object]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Nie ma takiego zadania.")
        return job.view()

    @app.get("/api/health")
    def health(session: Annotated[Session, Depends(get_session)]) -> dict[str, object]:
        offers = session.exec(select(func.count()).select_from(Offer)).one()
        cvs = session.exec(select(func.count()).select_from(CV)).one()
        return {"status": "ok", "version": __version__, "offers": offers, "cvs": cvs}

    @app.get("/api/settings")
    def read_settings() -> dict[str, object]:
        return settings.public_view()

    return app
