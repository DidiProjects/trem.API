import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.api.middleware.body_size_limit import BodySizeLimitMiddleware
from app.api.v1.routers import admin as admin_router
from app.api.v1.routers import api_clients as api_clients_router
from app.api.v1.routers import hub as hub_router
from app.api.v1.routers import me as me_router
from app.core.config import get_settings
from app.routers import (
    audioRoute,
    imageRoute,
    llmRoute,
    pdfRoute,
    scraperRoute,
    supportRoute,
    transcriptionRoute,
    videoRoute,
)
from app.services.transcriptionService import get_transcription_jobs, sweep_periodically

# uvicorn configures only its own loggers; without a handler here, INFO lines
# from app.* never reach the container logs.
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
_app_logger.addHandler(_handler)

_SWEEP_INTERVAL_SECONDS = 600


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Garante que o diretório de uploads existe
    settings = get_settings()
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

    jobs = get_transcription_jobs()
    jobs.reset_storage()
    sweeper = asyncio.create_task(sweep_periodically(jobs, _SWEEP_INTERVAL_SECONDS))
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    jobs.shutdown()


app = FastAPI(
    title="trem.API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    BodySizeLimitMiddleware,
    default_limit=get_settings().MAX_REQUEST_SIZE,
    limits_by_prefix={
        "/audio": get_settings().MAX_MEDIA_REQUEST_SIZE,
        "/movie": get_settings().MAX_MEDIA_REQUEST_SIZE,
        "/llm": get_settings().LLM_MAX_REQUEST_SIZE,
        "/scrapers": get_settings().SCRAPER_MAX_REQUEST_SIZE,
    },
)
# Added last, so it wraps the others: their error responses (413 and the like) carry the
# CORS headers too, or the browser hides them from the front as a network error.
if get_settings().cors_origins():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        expose_headers=["Retry-After"],
        max_age=600,
    )

# --- Routers v1: autenticação e gestão de usuários ---
app.include_router(me_router.router)
app.include_router(hub_router.router)
app.include_router(hub_router.admin_router)
app.include_router(api_clients_router.router)
app.include_router(admin_router.router)

# --- Routers de processamento de arquivos (mantidos) ---
app.include_router(pdfRoute.router, prefix="/pdf", tags=["PDF"])
app.include_router(videoRoute.router, prefix="/movie", tags=["Movies"])
app.include_router(transcriptionRoute.router, prefix="/movie", tags=["Movies"])
app.include_router(audioRoute.router, prefix="/audio", tags=["Audio"])
app.include_router(transcriptionRoute.router, prefix="/audio", tags=["Audio"])
app.include_router(imageRoute.router, prefix="/image", tags=["Image"])
app.include_router(supportRoute.router, prefix="/support", tags=["Support"])
app.include_router(llmRoute.router, prefix="/llm", tags=["LLM"])
app.include_router(scraperRoute.router, prefix="/scrapers/linkedin", tags=["Scrapers"])


@app.get("/", include_in_schema=False)
async def root():
    """The only page is the interactive documentation."""
    return RedirectResponse("/docs")


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
