import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .settings import settings
from .cache.db import init_db, close_db
from .api.health import router as health_router
from .api.match import router as match_router
from .api.featurization import router as featurization_router
from .matching import worker as featurize_worker


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup: STASH_URL=%s EXTRACTOR_URL=%s DATA_DIR=%s lifecycle=%s",
                settings.stash_url, settings.extractor_url, settings.data_dir,
                settings.bridge_lifecycle_enabled)
    await init_db()
    if settings.bridge_lifecycle_enabled:
        await featurize_worker.startup_recover()
        await featurize_worker.start_lru_eviction_loop()
    yield
    if settings.bridge_lifecycle_enabled:
        await featurize_worker.shutdown()
    await close_db()


app = FastAPI(
    title="stash-extract-db",
    description="Bridge between Stash and Site Extractor for scene metadata matching",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(match_router)
app.include_router(featurization_router)
