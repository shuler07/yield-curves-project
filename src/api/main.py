import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from src.api.routes import router
from src.api.settings import Settings
from src.etl.scheduler import EtlScheduler

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = EtlScheduler(settings) if settings.scheduler_enabled else None
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await asyncio.to_thread(scheduler.stop)

    app = FastAPI(title="Yield Curves API", version="1.0.0", lifespan=lifespan)
    app.include_router(router)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        LOGGER.error("Database error serving %s", request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "База данных недоступна или схема не обновлена"},
        )

    return app


app = create_app()
