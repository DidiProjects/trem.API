import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.routers import auth as auth_router
from app.api.v1.routers import users as users_router
from app.core.config import get_settings
from app.routers import audioRoute, imageRoute, pdfRoute, supportRoute, videoRoute

static_dir = Path(__file__).parent / "static"


def _file_hash(path: Path) -> str:
    if path.exists():
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]
    return ""


_static_hashes = {
    "style.css": _file_hash(static_dir / "style.css"),
    "script.js": _file_hash(static_dir / "script.js"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Garante que o diretório de uploads existe
    settings = get_settings()
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    yield


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
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# --- Routers v1: autenticação e gestão de usuários ---
app.include_router(auth_router.router)
app.include_router(users_router.router)

# --- Routers de processamento de arquivos (mantidos) ---
app.include_router(pdfRoute.router, prefix="/pdf", tags=["PDF"])
app.include_router(videoRoute.router, prefix="/movie", tags=["Movies"])
app.include_router(audioRoute.router, prefix="/audio", tags=["Audio"])
app.include_router(imageRoute.router, prefix="/image", tags=["Image"])
app.include_router(supportRoute.router, prefix="/support", tags=["Support"])


@app.get("/", include_in_schema=False)
async def root():
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="/static/style.css"',
        f'href="/static/style.css?v={_static_hashes["style.css"]}"',
    )
    html = html.replace(
        'src="/static/script.js"',
        f'src="/static/script.js?v={_static_hashes["script.js"]}"',
    )
    return HTMLResponse(content=html)


@app.get("/config", include_in_schema=False)
async def get_config():
    settings = get_settings()
    return {"api_url": settings.API_URL}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
