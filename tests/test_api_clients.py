"""
Testes da autenticação por cliente: modelo, repositório, serviço, geração de
chave e a resolução de Principal usada por require_profile.

O repositório roda contra SQLite real; a autorização roda via HTTP.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.v1.dependencies import get_current_principal, require_profile
from app.domain.entities.api_client import (
    API_KEY_PREFIX,
    ApiClient,
    Principal,
    PrincipalKind,
)
from app.domain.entities.profile import ProfileName
from app.infrastructure.database.connection import get_db
from app.infrastructure.database.models import ApiClientModel
from app.repositories.api_client_repository import ApiClientRepository
from app.services.api_client_service import (
    ApiClientAlreadyExistsError,
    ApiClientNotFoundError,
    ApiClientService,
    generate_api_key,
    hash_api_key,
)
from tests.conftest import make_mock_user, mock_get_db


@pytest.fixture
def repo(db_session):
    return ApiClientRepository(db_session)


@pytest.fixture
def service(db_session):
    return ApiClientService(db_session)


# ---------------------------------------------------------------------------
# Geração e hashing da chave
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_has_identifiable_prefix(self):
        assert generate_api_key().startswith(API_KEY_PREFIX)

    def test_is_long_enough_to_be_unguessable(self):
        # 32 bytes em base64url ~ 43 chars, mais o prefixo
        assert len(generate_api_key()) >= len(API_KEY_PREFIX) + 40

    def test_every_key_is_unique(self):
        assert len({generate_api_key() for _ in range(200)}) == 200

    def test_is_url_safe(self):
        import string

        allowed = set(string.ascii_letters + string.digits + "-_" + API_KEY_PREFIX)
        assert set(generate_api_key()) <= allowed


class TestKeyHashing:
    def test_is_deterministic(self):
        """Determinístico de propósito: é o que permite lookup por índice."""
        key = generate_api_key()
        assert hash_api_key(key) == hash_api_key(key)

    def test_returns_sha256_hex(self):
        digest = hash_api_key("trem_abc")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_different_keys_give_different_hashes(self):
        assert hash_api_key("trem_a") != hash_api_key("trem_b")

    def test_hash_does_not_contain_key(self):
        key = generate_api_key()
        assert key not in hash_api_key(key)

    def test_matches_hashlib(self):
        import hashlib

        assert hash_api_key("trem_x") == hashlib.sha256(b"trem_x").hexdigest()


# ---------------------------------------------------------------------------
# Repositório
# ---------------------------------------------------------------------------

class TestApiClientRepository:
    async def _create(self, repo, seeded_profiles, name="cliente", **kwargs):
        params = {
            "name": name,
            "key_hash": hash_api_key(f"key-{name}"),
            "profile_id": seeded_profiles[ProfileName.FILE_EDITOR.value],
        }
        params.update(kwargs)
        return await repo.create(**params)

    async def test_create_returns_entity(self, repo, seeded_profiles):
        client = await self._create(repo, seeded_profiles)
        assert isinstance(client, ApiClient)
        assert client.name == "cliente"
        assert client.profile_name == ProfileName.FILE_EDITOR.value

    async def test_create_defaults_to_active(self, repo, seeded_profiles):
        client = await self._create(repo, seeded_profiles)
        assert client.revoked is False
        assert client.is_active is True
        assert client.last_used_at is None

    async def test_get_by_key_hash(self, repo, seeded_profiles):
        await self._create(repo, seeded_profiles, "flight-api")
        found = await repo.get_by_key_hash(hash_api_key("key-flight-api"))
        assert found is not None
        assert found.name == "flight-api"

    async def test_get_by_key_hash_unknown_returns_none(self, repo, seeded_profiles):
        assert await repo.get_by_key_hash(hash_api_key("nao-existe")) is None

    async def test_get_by_name(self, repo, seeded_profiles):
        await self._create(repo, seeded_profiles, "scraping-api")
        assert (await repo.get_by_name("scraping-api")) is not None

    async def test_get_by_id_malformed_returns_none(self, repo, seeded_profiles):
        assert await repo.get_by_id("nao-e-uuid") is None

    async def test_revoke(self, repo, seeded_profiles):
        client = await self._create(repo, seeded_profiles)
        await repo.revoke(client.id)
        assert (await repo.get_by_id(client.id)).revoked is True

    async def test_revoke_does_not_touch_others(self, repo, seeded_profiles):
        a = await self._create(repo, seeded_profiles, "cliente-a")
        b = await self._create(repo, seeded_profiles, "cliente-b")
        await repo.revoke(a.id)
        assert (await repo.get_by_id(b.id)).revoked is False

    async def test_touch_last_used(self, repo, seeded_profiles):
        client = await self._create(repo, seeded_profiles)
        await repo.touch_last_used(client.id, datetime.now(timezone.utc))
        assert (await repo.get_by_id(client.id)).last_used_at is not None

    async def test_name_is_unique(self, db_session, repo, seeded_profiles):
        await self._create(repo, seeded_profiles, "duplicado")
        with pytest.raises(IntegrityError):
            await repo.create(
                name="duplicado",
                key_hash=hash_api_key("outra-chave"),
                profile_id=seeded_profiles[ProfileName.FILE_EDITOR.value],
            )

    async def test_key_hash_is_unique(self, db_session, repo, seeded_profiles):
        """Duas linhas com o mesmo hash tornariam o lookup ambíguo."""
        await self._create(repo, seeded_profiles, "cliente-a")
        with pytest.raises(IntegrityError):
            await repo.create(
                name="cliente-b",
                key_hash=hash_api_key("key-cliente-a"),
                profile_id=seeded_profiles[ProfileName.FILE_EDITOR.value],
            )

    async def test_list_and_count(self, repo, seeded_profiles):
        for i in range(4):
            await self._create(repo, seeded_profiles, f"cliente{i}")
        page = await repo.list_all(limit=2)
        assert len(page) == 2
        assert await repo.count_all() == 4

    async def test_repr_hides_key_hash(self, db_session, seeded_profiles):
        orm = ApiClientModel(
            name="secreto",
            key_hash="hash-super-secreto",
            profile_id=uuid.UUID(seeded_profiles[ProfileName.FILE_EDITOR.value]),
        )
        assert "hash-super-secreto" not in repr(orm)


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------

class TestApiClientService:
    async def test_create_returns_client_and_raw_key(self, service, seeded_profiles):
        client, raw_key = await service.create_client(
            name="flight-api", profile_name=ProfileName.AIRLINE_COMPANY.value
        )
        assert client.name == "flight-api"
        assert raw_key.startswith(API_KEY_PREFIX)

    async def test_create_assigns_requested_profile(self, service, seeded_profiles):
        client, _ = await service.create_client(
            name="flight-api", profile_name=ProfileName.AIRLINE_COMPANY.value
        )
        assert client.profile_name == ProfileName.AIRLINE_COMPANY.value

    async def test_duplicate_name_raises(self, service, seeded_profiles):
        await service.create_client(name="dup", profile_name="file_editor")
        with pytest.raises(ApiClientAlreadyExistsError):
            await service.create_client(name="dup", profile_name="file_editor")

    async def test_unknown_profile_raises(self, service, seeded_profiles):
        from app.core.exceptions import AppException

        with pytest.raises(AppException):
            await service.create_client(name="x", profile_name="inexistente")

    async def test_authenticate_accepts_issued_key(self, service, seeded_profiles):
        _, raw_key = await service.create_client(
            name="playground", profile_name="file_editor"
        )
        client = await service.authenticate(raw_key)
        assert client is not None
        assert client.name == "playground"

    async def test_authenticate_rejects_unknown_key(self, service, seeded_profiles):
        assert await service.authenticate(generate_api_key()) is None

    async def test_authenticate_rejects_empty_key(self, service, seeded_profiles):
        assert await service.authenticate("") is None

    async def test_authenticate_rejects_revoked_key(self, service, seeded_profiles):
        client, raw_key = await service.create_client(
            name="antigo", profile_name="file_editor"
        )
        await service.revoke_client(client.id)
        assert await service.authenticate(raw_key) is None

    async def test_authenticate_records_last_used(self, service, seeded_profiles):
        client, raw_key = await service.create_client(
            name="usado", profile_name="file_editor"
        )
        assert client.last_used_at is None

        await service.authenticate(raw_key)
        assert (await service.get_client(client.id)).last_used_at is not None

    async def test_raw_key_is_not_recoverable(self, service, seeded_profiles):
        """A chave em claro só existe no retorno da criação."""
        client, raw_key = await service.create_client(
            name="efemera", profile_name="file_editor"
        )
        stored = await service.get_client(client.id)
        assert raw_key not in str(stored.__dict__)

    async def test_each_client_gets_a_different_key(self, service, seeded_profiles):
        _, key_a = await service.create_client(name="a", profile_name="file_editor")
        _, key_b = await service.create_client(name="b", profile_name="file_editor")
        assert key_a != key_b

    async def test_revoke_unknown_raises(self, service, seeded_profiles):
        with pytest.raises(ApiClientNotFoundError):
            await service.revoke_client(str(uuid.uuid4()))

    async def test_get_unknown_raises(self, service, seeded_profiles):
        with pytest.raises(ApiClientNotFoundError):
            await service.get_client(str(uuid.uuid4()))

    async def test_list_returns_page_and_total(self, service, seeded_profiles):
        for i in range(3):
            await service.create_client(name=f"c{i}", profile_name="file_editor")
        clients, total = await service.list_clients(limit=2)
        assert len(clients) == 2
        assert total == 3


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------

class TestPrincipal:
    def _client(self, profile=ProfileName.FILE_EDITOR.value):
        return ApiClient(
            id="c-1",
            name="flight-api",
            description=None,
            profile_id="p-1",
            profile_name=profile,
            revoked=False,
            last_used_at=None,
            created_at=datetime.now(timezone.utc),
        )

    def test_from_client(self):
        p = Principal.from_client(self._client())
        assert p.kind == PrincipalKind.CLIENT
        assert p.is_client is True
        assert p.is_user is False
        assert p.display_name == "flight-api"

    def test_client_never_needs_password_change(self):
        """Cliente não tem senha — a regra de troca não pode se aplicar a ele."""
        assert Principal.from_client(self._client()).must_change_password is False

    def test_from_user(self):
        p = Principal.from_user(make_mock_user())
        assert p.kind == PrincipalKind.USER
        assert p.is_user is True
        assert p.display_name == "testuser"

    def test_from_user_carries_password_flag(self):
        p = Principal.from_user(make_mock_user(must_change_password=True))
        assert p.must_change_password is True

    def test_client_repr_hides_nothing_sensitive(self):
        assert "flight-api" in repr(self._client())


# ---------------------------------------------------------------------------
# Autorização por API key, via HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def probe_app():
    api = FastAPI()

    @api.get("/editor")
    async def editor(p: Principal = Depends(require_profile("file_editor"))):
        return {"name": p.display_name, "kind": p.kind.value}

    @api.get("/quem-sou")
    async def quem_sou(p: Principal = Depends(get_current_principal)):
        return {"name": p.display_name, "kind": p.kind.value}

    api.dependency_overrides[get_db] = mock_get_db
    return api


@pytest.fixture
def client_service():
    mock = MagicMock()
    mock.authenticate = AsyncMock(return_value=None)
    with patch("app.api.v1.dependencies.ApiClientService", return_value=mock):
        yield mock


def _api_client(profile=ProfileName.FILE_EDITOR.value, name="flight-api"):
    return ApiClient(
        id="c-1",
        name=name,
        description=None,
        profile_id="p-1",
        profile_name=profile,
        revoked=False,
        last_used_at=None,
        created_at=datetime.now(timezone.utc),
    )


class TestApiKeyAuthorization:
    def test_valid_key_is_accepted(self, probe_app, client_service):
        client_service.authenticate.return_value = _api_client()
        response = TestClient(probe_app).get(
            "/editor", headers={"X-API-Key": "trem_chave-valida"}
        )
        assert response.status_code == 200
        assert response.json() == {"name": "flight-api", "kind": "client"}

    def test_invalid_key_returns_401(self, probe_app, client_service):
        client_service.authenticate.return_value = None
        response = TestClient(probe_app).get(
            "/editor", headers={"X-API-Key": "trem_chave-invalida"}
        )
        assert response.status_code == 401

    def test_revoked_key_returns_401(self, probe_app, client_service):
        """Serviço devolve None para chave revogada — vira 401 na borda."""
        client_service.authenticate.return_value = None
        response = TestClient(probe_app).get(
            "/editor", headers={"X-API-Key": "trem_revogada"}
        )
        assert response.status_code == 401
        assert "revogada" in response.json()["detail"].lower()

    def test_wrong_profile_returns_403(self, probe_app, client_service):
        client_service.authenticate.return_value = _api_client(
            profile=ProfileName.AIRLINE_COMPANY.value
        )
        response = TestClient(probe_app).get(
            "/editor", headers={"X-API-Key": "trem_chave-valida"}
        )
        assert response.status_code == 403

    def test_no_credential_returns_401(self, probe_app, client_service):
        response = TestClient(probe_app).get("/editor")
        assert response.status_code == 401

    def test_error_message_mentions_both_schemes(self, probe_app, client_service):
        detail = TestClient(probe_app).get("/editor").json()["detail"]
        assert "X-API-Key" in detail
        assert "Bearer" in detail

    def test_key_is_looked_up_verbatim(self, probe_app, client_service):
        client_service.authenticate.return_value = _api_client()
        TestClient(probe_app).get("/editor", headers={"X-API-Key": "trem_abc123"})
        client_service.authenticate.assert_awaited_once_with("trem_abc123")

    def test_profile_comes_from_record_not_from_header(self, probe_app, client_service):
        """O chamador não consegue forjar o perfil — ele vem do banco."""
        client_service.authenticate.return_value = _api_client(
            profile=ProfileName.AIRLINE_COMPANY.value
        )
        response = TestClient(probe_app).get(
            "/editor",
            headers={"X-API-Key": "trem_x", "X-Profile": "file_editor"},
        )
        assert response.status_code == 403


class TestPrincipalRouting:
    def test_api_key_takes_precedence_over_bearer(self, probe_app, client_service):
        """Com as duas credenciais presentes, a API key decide."""
        client_service.authenticate.return_value = _api_client(name="via-chave")
        response = TestClient(probe_app).get(
            "/quem-sou",
            headers={"X-API-Key": "trem_x", "Authorization": "Bearer qualquer.coisa"},
        )
        assert response.json()["kind"] == "client"
        assert response.json()["name"] == "via-chave"
