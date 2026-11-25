"""Aplikacja FastAPI: pusty panel i podstawowe API."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from cv_app import __version__
from cv_app.config import Settings, get_settings
from cv_app.db import init_db, make_engine
from cv_app.models import CV, Offer

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(engine)
        yield
        engine.dispose()

    app = FastAPI(title="CV app", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def get_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"settings": settings.public_view(), "version": __version__},
        )

    @app.get("/api/health")
    def health(session: Annotated[Session, Depends(get_session)]) -> dict[str, object]:
        offers = session.exec(select(func.count()).select_from(Offer)).one()
        cvs = session.exec(select(func.count()).select_from(CV)).one()
        return {"status": "ok", "version": __version__, "offers": offers, "cvs": cvs}

    @app.get("/api/settings")
    def read_settings() -> dict[str, object]:
        return settings.public_view()

    return app
