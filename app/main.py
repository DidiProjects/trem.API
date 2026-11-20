import asyncio
import contextlib
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.middleware.body_size_limit import BodySizeLimitMiddleware
from app.api.v1.routers import admin as admin_router
from app.api.v1.routers import api_clients as api_clients_router
from app.api.v1.routers import auth as auth_router
from app.api.v1.routers import hub as hub_router
from app.api.v1.routers import oauth as oauth_router
from app.api.v1.routers import me as me_router
from app.api.v1.routers import signup as signup_router
from app.api.v1.routers import users as users_router
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

static_dir = Path(__file__).parent / "static"


def _file_hash(path: Path) -> str:
    if path.exists():
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]
    return ""


_static_hashes = {
    "style.css": _file_hash(static_dir / "style.css"),
    "account.js": _file_hash(static_dir / "account.js"),
    "script.js": _file_hash(static_dir / "script.js"),
}


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


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            if "?" in str(request.url):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response


app = FastAPI(
    title="trem.API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(CacheControlMiddleware)
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
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# --- Routers v1: autenticação e gestão de usuários ---
app.include_router(auth_router.router)
app.include_router(auth_router.well_known_router)
app.include_router(signup_router.router)
app.include_router(me_router.router)
app.include_router(hub_router.router)
app.include_router(hub_router.admin_router)
app.include_router(oauth_router.router)
app.include_router(oauth_router.well_known_router)
app.include_router(users_router.router)
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
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="/static/style.css"',
        f'href="/static/style.css?v={_static_hashes["style.css"]}"',
    )
    for script in ("account.js", "script.js"):
        html = html.replace(
            f'src="/static/{script}"',
            f'src="/static/{script}?v={_static_hashes[script]}"',
        )
    return HTMLResponse(content=html)


@app.get("/config", include_in_schema=False)
async def get_config():
    settings = get_settings()
    return {"api_url": settings.API_URL}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
