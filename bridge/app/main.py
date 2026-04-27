import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .settings import settings
from .cache.db import init_db, close_db
from .api.health import router as health_router
from .api.match import router as match_router


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup: STASH_URL=%s EXTRACTOR_URL=%s DATA_DIR=%s",
                settings.stash_url, settings.extractor_url, settings.data_dir)
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="stash-extract-db",
    description="Bridge between Stash and Site Extractor for scene metadata matching",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(match_router)
