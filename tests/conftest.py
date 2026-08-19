import io
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Configuração de ambiente de teste
#
# Precisa acontecer ANTES de qualquer import de app.*, porque get_settings() é
# lru_cache: a primeira chamada congela os valores. Gerar o par RSA aqui deixa
# os testes de JWT independentes do .env real da máquina.
# ---------------------------------------------------------------------------

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PRIVATE_KEY = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
TEST_PUBLIC_KEY = (
    _key.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)

TEST_API_KEY = "test-api-key-do-not-use-in-prod"

os.environ.update(
    {
        "JWT_PRIVATE_KEY": TEST_PRIVATE_KEY,
        "JWT_PUBLIC_KEY": TEST_PUBLIC_KEY,
        "JWT_ALGORITHM": "RS256",
        "ACCESS_TOKEN_EXPIRE_MINUTES": "15",
        "REFRESH_TOKEN_EXPIRE_DAYS": "7",
        "API_KEY": TEST_API_KEY,
        "API_URL": "http://testserver",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
        "DEBUG": "False",
        # Limpa config de email/airline para os testes controlarem cada cenário
        "EMAIL_SMTP_USER": "",
        "EMAIL_SMTP_PASSWORD": "",
        "EMAIL_RECIPIENT": "",
        "AIRLINE_API_URL": "",
        "AIRLINE_API_KEY": "",
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
from app.api.v1.dependencies import get_current_user  # noqa: E402
from app.domain.entities.profile import ProfileName  # noqa: E402
from app.domain.entities.user import User, UserStatus  # noqa: E402
from app.infrastructure.database.connection import get_db  # noqa: E402
from app.infrastructure.database.models import (  # noqa: E402
    Base,
    ProfileModel,
    UserModel,
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
async def seeded_profiles(db_session):
    """Insere os dois perfis do seed da migration 001 e devolve {nome: id}."""
    profiles = {}
    for name, description in [
        (ProfileName.FILE_EDITOR, "Acesso aos serviços de manipulação de arquivos"),
        (ProfileName.AIRLINE_COMPANY, "Acesso a informações de empresas aéreas"),
    ]:
        orm = ProfileModel(id=uuid.uuid4(), name=name.value, description=description)
        db_session.add(orm)
        profiles[name.value] = str(orm.id)
    await db_session.flush()
    return profiles


@pytest.fixture
async def persisted_user(db_session, seeded_profiles):
    """Um usuário ativo já gravado no SQLite, com profile carregado."""
    orm = UserModel(
        id=uuid.uuid4(),
        username="persisted",
        email="persisted@example.com",
        password_hash="$argon2id$placeholder",
        profile_id=uuid.UUID(seeded_profiles[ProfileName.FILE_EDITOR.value]),
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
    )
    db_session.add(orm)
    await db_session.flush()
    await db_session.refresh(orm, ["profile"])
    return orm


# ---------------------------------------------------------------------------
# Usuários mock
# ---------------------------------------------------------------------------

def make_mock_user(
    profile_name: str = ProfileName.FILE_EDITOR.value,
    must_change_password: bool = False,
    status: str = UserStatus.ACTIVE.value,
) -> User:
    return User(
        id="00000000-0000-0000-0000-000000000001",
        username="testuser",
        email="test@example.com",
        password_hash="$argon2id$placeholder",
        profile_id="00000000-0000-0000-0000-000000000010",
        profile_name=profile_name,
        status=status,
        must_change_password=must_change_password,
        provisional_password_sent_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_login_at=None,
    )


async def _user_file_editor():
    return make_mock_user(ProfileName.FILE_EDITOR.value)


async def _user_must_change():
    return make_mock_user(ProfileName.FILE_EDITOR.value, must_change_password=True)


async def _user_airline():
    return make_mock_user(ProfileName.AIRLINE_COMPANY.value)


# ---------------------------------------------------------------------------
# Fixtures de client HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Client autenticado como file_editor (para testes de routers de arquivo)."""
    app.dependency_overrides[get_current_user] = _user_file_editor
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_must_change():
    """Client autenticado, mas com must_change_password=True."""
    app.dependency_overrides[get_current_user] = _user_must_change
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_airline():
    """Client autenticado com perfil airline_company."""
    app.dependency_overrides[get_current_user] = _user_airline
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_auth():
    """Client sem autenticação (DB mockado para evitar erro de conexão)."""
    app.dependency_overrides[get_db] = mock_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


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
