"""
Testes de AuthService — a máquina de estados de login/refresh/logout.

O repositório é mockado, mas o hashing e a assinatura JWT são reais: é o que
garante que a regra de negócio e a criptografia continuam casando.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import InvalidCredentialsError, UserBlockedError
from app.core.security import create_access_token, create_refresh_token, hash_password, hash_token
from app.domain.entities.user import UserStatus
from app.services.auth_service import AuthService
from tests.conftest import make_mock_user

SENHA = "SenhaValida@123"
SENHA_HASH = hash_password(SENHA)  # calculado uma vez: Argon2id é caro de propósito


@pytest.fixture
def repo():
    """Mock do UserRepository com todos os métodos usados pelo AuthService."""
    mock = MagicMock()
    mock.get_by_id = AsyncMock(return_value=None)
    mock.get_by_username = AsyncMock(return_value=None)
    mock.update_password = AsyncMock()
    mock.update_last_login = AsyncMock()
    mock.create_refresh_token = AsyncMock()
    mock.get_refresh_token = AsyncMock(return_value=None)
    mock.revoke_refresh_token = AsyncMock()
    mock.revoke_all_refresh_tokens = AsyncMock()
    return mock


@pytest.fixture
def service(repo):
    with patch("app.services.auth_service.UserRepository", return_value=repo):
        yield AuthService(MagicMock())


def _user(**kwargs):
    user = make_mock_user(**kwargs)
    user.password_hash = SENHA_HASH
    return user


def _stored_token(revoked=False, expired=False):
    return SimpleNamespace(
        revoked=revoked,
        expires_at=datetime.now(timezone.utc)
        + (timedelta(days=-1) if expired else timedelta(days=7)),
    )


class TestLogin:
    async def test_success_returns_tokens(self, service, repo):
        repo.get_by_username.return_value = _user()

        result = await service.login("testuser", SENHA)

        assert result.access_token
        assert result.refresh_token
        assert result.token_type == "bearer"
        assert result.must_change_password is False

    async def test_success_persists_refresh_token_hash_not_raw(self, service, repo):
        """O banco nunca pode guardar o refresh token em claro."""
        repo.get_by_username.return_value = _user()

        result = await service.login("testuser", SENHA)

        stored_hash = repo.create_refresh_token.await_args.kwargs["token_hash"]
        assert stored_hash == hash_token(result.refresh_token)
        assert stored_hash != result.refresh_token

    async def test_success_records_last_login(self, service, repo):
        repo.get_by_username.return_value = _user()
        await service.login("testuser", SENHA)
        repo.update_last_login.assert_awaited_once()

    async def test_success_stores_ip_and_user_agent(self, service, repo):
        repo.get_by_username.return_value = _user()

        await service.login("testuser", SENHA, ip_address="203.0.113.9", user_agent="curl/8")

        kwargs = repo.create_refresh_token.await_args.kwargs
        assert kwargs["ip_address"] == "203.0.113.9"
        assert kwargs["user_agent"] == "curl/8"

    async def test_propagates_must_change_password(self, service, repo):
        repo.get_by_username.return_value = _user(must_change_password=True)
        result = await service.login("testuser", SENHA)
        assert result.must_change_password is True

    async def test_unknown_user_raises_invalid_credentials(self, service, repo):
        repo.get_by_username.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.login("ninguem", SENHA)

    async def test_wrong_password_raises_invalid_credentials(self, service, repo):
        repo.get_by_username.return_value = _user()
        with pytest.raises(InvalidCredentialsError):
            await service.login("testuser", "senha-errada")

    async def test_unknown_user_and_wrong_password_give_same_error(self, service, repo):
        """Mesma mensagem nos dois casos — evita enumeração de usuários."""
        repo.get_by_username.return_value = None
        with pytest.raises(InvalidCredentialsError) as unknown:
            await service.login("ninguem", SENHA)

        repo.get_by_username.return_value = _user()
        with pytest.raises(InvalidCredentialsError) as wrong:
            await service.login("testuser", "senha-errada")

        assert unknown.value.message == wrong.value.message

    async def test_blocked_user_raises_user_blocked(self, service, repo):
        repo.get_by_username.return_value = _user(status=UserStatus.BLOCKED.value)
        with pytest.raises(UserBlockedError):
            await service.login("testuser", SENHA)

    async def test_suspended_user_raises_user_blocked(self, service, repo):
        repo.get_by_username.return_value = _user(status=UserStatus.SUSPENDED.value)
        with pytest.raises(UserBlockedError):
            await service.login("testuser", SENHA)

    async def test_blocked_user_gets_no_refresh_token(self, service, repo):
        repo.get_by_username.return_value = _user(status=UserStatus.BLOCKED.value)
        with pytest.raises(UserBlockedError):
            await service.login("testuser", SENHA)
        repo.create_refresh_token.assert_not_awaited()

    async def test_rehashes_password_when_parameters_are_outdated(self, service, repo):
        from argon2 import PasswordHasher

        user = _user()
        user.password_hash = PasswordHasher(
            time_cost=1, memory_cost=8, parallelism=1
        ).hash(SENHA)
        repo.get_by_username.return_value = user

        await service.login("testuser", SENHA)

        repo.update_password.assert_awaited_once()

    async def test_does_not_rehash_when_parameters_are_current(self, service, repo):
        repo.get_by_username.return_value = _user()
        await service.login("testuser", SENHA)
        repo.update_password.assert_not_awaited()

    async def test_access_token_carries_profile(self, service, repo):
        from app.core.security import decode_token

        repo.get_by_username.return_value = _user(profile_name="airline_company")
        result = await service.login("testuser", SENHA)

        assert decode_token(result.access_token)["profile"] == "airline_company"


class TestRefresh:
    async def test_success_returns_new_access_token(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user()

        result = await service.refresh(raw)

        assert result.access_token
        assert result.token_type == "bearer"

    async def test_looks_up_token_by_hash(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user()

        await service.refresh(raw)

        repo.get_refresh_token.assert_awaited_once_with(hash_token(raw))

    async def test_garbage_token_raises_invalid_credentials(self, service, repo):
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("nao-e-um-jwt")

    async def test_access_token_is_rejected(self, service, repo):
        """Um access token não pode ser usado para renovar sessão."""
        access = create_access_token(
            "00000000-0000-0000-0000-000000000001", "testuser", "file_editor", False
        )
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(access)

    async def test_unknown_stored_token_raises(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(raw)

    async def test_revoked_token_raises(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token(revoked=True)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(raw)

    async def test_expired_stored_token_raises(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token(expired=True)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(raw)

    async def test_deleted_user_raises(self, service, repo):
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(raw)

    async def test_blocked_user_raises(self, service, repo):
        """Bloquear a conta precisa invalidar a renovação em curso."""
        raw, _ = create_refresh_token("00000000-0000-0000-0000-000000000001")
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user(status=UserStatus.BLOCKED.value)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh(raw)


class TestChangePassword:
    async def test_success_updates_hash(self, service, repo):
        repo.get_by_id.return_value = _user()

        await service.change_password("id-1", SENHA, "NovaSenha@456")

        kwargs = repo.update_password.await_args.kwargs
        assert kwargs["must_change_password"] is False
        assert kwargs["password_hash"] != SENHA_HASH

    async def test_success_stores_a_verifiable_hash(self, service, repo):
        from app.core.security import verify_password

        repo.get_by_id.return_value = _user()
        await service.change_password("id-1", SENHA, "NovaSenha@456")

        novo_hash = repo.update_password.await_args.kwargs["password_hash"]
        assert verify_password("NovaSenha@456", novo_hash) is True

    async def test_success_revokes_all_refresh_tokens(self, service, repo):
        """Trocar senha precisa derrubar todas as sessões abertas."""
        repo.get_by_id.return_value = _user()
        await service.change_password("id-1", SENHA, "NovaSenha@456")
        repo.revoke_all_refresh_tokens.assert_awaited_once_with("id-1")

    async def test_wrong_current_password_raises(self, service, repo):
        repo.get_by_id.return_value = _user()
        with pytest.raises(InvalidCredentialsError):
            await service.change_password("id-1", "errada", "NovaSenha@456")

    async def test_wrong_current_password_does_not_update(self, service, repo):
        repo.get_by_id.return_value = _user()
        with pytest.raises(InvalidCredentialsError):
            await service.change_password("id-1", "errada", "NovaSenha@456")
        repo.update_password.assert_not_awaited()

    async def test_unknown_user_raises(self, service, repo):
        repo.get_by_id.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.change_password("id-1", SENHA, "NovaSenha@456")

    async def test_works_for_user_that_must_change_password(self, service, repo):
        repo.get_by_id.return_value = _user(must_change_password=True)
        await service.change_password("id-1", SENHA, "NovaSenha@456")
        assert repo.update_password.await_args.kwargs["must_change_password"] is False


class TestLogout:
    async def test_revokes_token_by_hash(self, service, repo):
        await service.logout("meu-refresh-token")
        repo.revoke_refresh_token.assert_awaited_once_with(hash_token("meu-refresh-token"))

    async def test_unknown_token_does_not_raise(self, service, repo):
        """Logout é idempotente — token desconhecido não deve estourar erro."""
        await service.logout("token-inexistente")
        repo.revoke_refresh_token.assert_awaited_once()
