import io
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Configuração de ambiente de teste
#
# Precisa acontecer ANTES de qualquer import de app.*, porque get_settings() é
# lru_cache: a primeira chamada congela os valores.
# ---------------------------------------------------------------------------

os.environ.update(
    {
        "API_URL": "http://testserver",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
        "DEBUG": "False",
        # Limpa config de email/airline para os testes controlarem cada cenário
        "EMAIL_SMTP_USER": "",
        "EMAIL_SMTP_PASSWORD": "",
        "EMAIL_RECIPIENT": "",
        "AIRLINE_API_URL": "",
        "AIRLINE_API_KEY": "",
        # The CORS middleware is installed at import; test_cors checks it with this origin.
        "CORS_ORIGINS": "https://front.example, http://localhost:3004/",
    }
)

import pikepdf  # noqa: E402
import pymupdf as fitz  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.api.v1.dependencies import (  # noqa: E402
    authenticated_user_or_none,
    get_current_user,
)
from app.domain.entities.permission import TREM_APP, TremPermission  # noqa: E402
from app.domain.entities.user import User  # noqa: E402
from app.infrastructure.database.connection import get_db  # noqa: E402
from app.infrastructure.database.models import (  # noqa: E402
    ApplicationModel,
    Base,
    PermissionModel,
    UserModel,
)
from app.services.transcriptionService import (  # noqa: E402
    TranscriptionJobs,
    get_transcription_jobs,
)


# ---------------------------------------------------------------------------
# DB mock — evita conexão real com PostgreSQL nos testes de rota
# ---------------------------------------------------------------------------

async def mock_get_db():
    yield MagicMock(spec=AsyncSession)


# ---------------------------------------------------------------------------
# DB real em SQLite — usado pelos testes de repositório/modelos
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_engine():
    """
    Engine SQLite in-memory compartilhada por uma única conexão (StaticPool),
    para que todas as sessões do teste enxerguem as mesmas tabelas.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded_permissions(db_session):
    """Insere a aplicação trem e as permissões do seed das migrations; devolve {chave: id}."""
    trem = ApplicationModel(id=uuid.uuid4(), slug=TREM_APP, name="trem.API")
    db_session.add(trem)
    permissions = {}
    for key in TremPermission:
        orm = PermissionModel(id=uuid.uuid4(), application=trem, name=key.split(":", 1)[1])
        db_session.add(orm)
        permissions[key.value] = str(orm.id)
    await db_session.flush()
    return permissions


@pytest.fixture
async def persisted_user(db_session, seeded_permissions):
    """Um usuário já gravado no SQLite, com a permissão trem:file_editor."""
    file_editor = await db_session.get(
        PermissionModel, uuid.UUID(seeded_permissions[TremPermission.FILE_EDITOR])
    )
    orm = UserModel(
        id=uuid.uuid4(),
        username="persisted",
        email="persisted@example.com",
        permissions=[file_editor],
    )
    db_session.add(orm)
    await db_session.flush()
    return orm


# ---------------------------------------------------------------------------
# Usuários mock
# ---------------------------------------------------------------------------

def make_mock_user(permissions: tuple[str, ...] = (TremPermission.FILE_EDITOR.value,)) -> User:
    return User(
        id="00000000-0000-0000-0000-000000000001",
        username="testuser",
        email="test@example.com",
        permissions=list(permissions),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


async def _user_file_editor():
    return make_mock_user((TremPermission.FILE_EDITOR.value,))


async def _user_airline():
    return make_mock_user((TremPermission.AIRLINE_COMPANY.value,))


async def _user_ai():
    return make_mock_user((TremPermission.AI.value,))


# ---------------------------------------------------------------------------
# Fixtures de client HTTP
# ---------------------------------------------------------------------------

def _authenticated_as(user_factory):
    """
    Monta um TestClient autenticado substituindo apenas a *resolução* do
    usuário. As regras de permissão continuam sendo aplicadas
    pelo get_current_principal real — é o que mantém os testes de 403
    verificando a aplicação, e não o fixture.
    """
    app.dependency_overrides[get_current_user] = user_factory
    app.dependency_overrides[authenticated_user_or_none] = user_factory
    app.dependency_overrides[get_db] = mock_get_db
    return TestClient(app)


@pytest.fixture
def authentik_login(monkeypatch):
    """trem.API trusts tests/authentik_tokens.py as if it were authentik."""
    from app.core import authentik
    from tests.authentik_tokens import ISSUER, FakeJwks

    settings = get_settings()
    monkeypatch.setattr(settings, "AUTHENTIK_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "AUTHENTIK_AUDIENCE", "trem")
    jwks = FakeJwks()
    monkeypatch.setattr(authentik, "_jwks_client", lambda _url: jwks)
    return jwks


@pytest.fixture
def client():
    """Client autenticado como file_editor (para testes de routers de arquivo)."""
    yield _authenticated_as(_user_file_editor)
    app.dependency_overrides.clear()


@pytest.fixture
def client_airline():
    """Client autenticado com a permissão trem:airline_company."""
    yield _authenticated_as(_user_airline)
    app.dependency_overrides.clear()


@pytest.fixture
def client_ai():
    """Client autenticado só com a permissão trem:ai."""
    yield _authenticated_as(_user_ai)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_auth():
    """Client sem autenticação (DB mockado para evitar erro de conexão)."""
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def transcription_jobs(tmp_path):
    """Transcription jobs rooted in tmp_path, injected into the routes."""
    jobs = TranscriptionJobs(
        root=tmp_path / "transcriptions",
        part_size=4,
        max_size=1024,
        ttl_seconds=3600,
    )
    app.dependency_overrides[get_transcription_jobs] = lambda: jobs
    yield jobs
    jobs.shutdown()
    app.dependency_overrides.pop(get_transcription_jobs, None)


# ---------------------------------------------------------------------------
# Fixtures de arquivos PDF
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_pdf_bytes():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.add_blank_page(page_size=(612, 792))
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(buf)
    buf.seek(0)
    pdf.close()
    return buf.getvalue()


@pytest.fixture
def sample_pdf_with_text():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), "Test text for extraction")
    page.insert_text((100, 120), "Second line of text")
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def sample_bank_statement_pdf():
    doc = fitz.open()
    page = doc.new_page()
    y = 100
    for date, tipo, desc, valor in [
        ("15/01/2026", "PIX", "Pagamento João", "R$ 150,00"),
        ("16/01/2026", "TED", "Transferência Maria", "-R$ 200,00"),
        ("17/01/2026", "Depósito", "Salário", "R$ 5.000,00"),
    ]:
        page.insert_text((100, y), date)
        page.insert_text((100, y + 15), tipo)
        page.insert_text((100, y + 30), desc)
        page.insert_text((100, y + 45), valor)
        y += 70
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    doc.close()
    return buf.getvalue()


@pytest.fixture
def protected_pdf_bytes():
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(
        buf,
        encryption=pikepdf.Encryption(user="user123", owner="owner123", aes=True, R=6),
    )
    buf.seek(0)
    pdf.close()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures de imagem
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_png_bytes():
    from PIL import Image

    img = Image.new("RGB", (120, 80), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def sample_heic_bytes():
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    img = Image.new("RGB", (120, 80), (30, 120, 200))
    buf = io.BytesIO()
    img.save(buf, format="HEIF")
    return buf.getvalue()


@pytest.fixture
def sample_jpeg_bytes():
    from PIL import Image

    img = Image.new("RGB", (64, 64), (10, 120, 220))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@pytest.fixture
def sample_rgba_png_bytes():
    from PIL import Image

    img = Image.new("RGBA", (50, 50), (0, 255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def sample_svg_bytes():
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<rect width="100" height="100" fill="#3366cc"/></svg>'
    )
