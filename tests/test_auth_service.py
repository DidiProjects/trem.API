"""
Testes de AuthService — a máquina de estados de login/refresh/logout.

O repositório é mockado, mas o hashing e a assinatura JWT são reais: é o que
garante que a regra de negócio e a criptografia continuam casando. A rotação
contra um banco de verdade está em tests/test_auth.py::TestSessionEndToEnd.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import (
    ApplicationAccessDeniedError,
    InvalidCredentialsError,
    TooManyLoginAttemptsError,
    UserBlockedError,
)
from app.core.security import decode_token, hash_password, hash_token
from app.domain.entities.user import UserStatus
from app.services.auth_service import AuthService
from app.services.login_throttle import LoginThrottle
from tests.conftest import make_mock_user

SENHA = "SenhaValida@123"
SENHA_HASH = hash_password(SENHA)  # calculado uma vez: Argon2id é caro de propósito
TREM_ID = uuid.uuid4()
FAMILY = uuid.uuid4()


@pytest.fixture
def repo():
    """Mock do UserRepository com todos os métodos usados pelo AuthService."""
    mock = MagicMock()
    mock.get_by_id = AsyncMock(return_value=None)
    mock.get_by_username = AsyncMock(return_value=None)
    mock.update_password = AsyncMock()
    mock.update_last_login = AsyncMock()
    mock.create_refresh_token = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    mock.get_refresh_token = AsyncMock(return_value=None)
    mock.claim_refresh_token = AsyncMock(return_value=True)
    mock.set_refresh_token_successor = AsyncMock()
    mock.revoke_refresh_token_family = AsyncMock()
    mock.revoke_all_refresh_tokens = AsyncMock()
    return mock


@pytest.fixture
def permissions():
    mock = MagicMock()
    apps = {"trem": SimpleNamespace(id=TREM_ID, slug="trem"),
            "financas": SimpleNamespace(id=uuid.uuid4(), slug="financas")}
    mock.get_application = AsyncMock(side_effect=lambda slug: apps.get(slug))
    return mock


@pytest.fixture
def throttle():
    return LoginThrottle(per_username=3, per_ip=10, window_seconds=60)


@pytest.fixture
def session():
    mock = MagicMock()
    mock.commit = AsyncMock()
    return mock


@pytest.fixture
def service(repo, permissions, throttle, session):
    with patch("app.services.auth_service.UserRepository", return_value=repo), patch(
        "app.services.auth_service.PermissionRepository", return_value=permissions
    ):
        yield AuthService(session, throttle=throttle)


def _user(**kwargs):
    user = make_mock_user(**kwargs)
    user.password_hash = SENHA_HASH
    return user


def _stored_token(revoked=False, expired=False, replaced=False, application="trem"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id="00000000-0000-0000-0000-000000000001",
        family_id=FAMILY,
        revoked=revoked,
        replaced_by=uuid.uuid4() if replaced else None,
        application_id=TREM_ID,
        application=SimpleNamespace(slug=application),
        amr="pwd",
        expires_at=datetime.now(timezone.utc)
        + (timedelta(days=-1) if expired else timedelta(days=7)),
    )


class TestLogin:
    async def test_success_returns_tokens(self, service, repo):
        repo.get_by_username.return_value = _user()

        result, raw_refresh = await service.login("testuser", SENHA)

        assert result.access_token
        assert raw_refresh
        assert result.refresh_token is None  # the route decides where the refresh goes
        assert result.token_type == "bearer"
        assert result.expires_in == 900
        assert result.must_change_password is False

    async def test_success_persists_refresh_token_hash_not_raw(self, service, repo):
        """O banco nunca pode guardar o refresh token em claro."""
        repo.get_by_username.return_value = _user()

        _, raw_refresh = await service.login("testuser", SENHA)

        stored_hash = repo.create_refresh_token.await_args.kwargs["token_hash"]
        assert stored_hash == hash_token(raw_refresh)
        assert stored_hash != raw_refresh

    async def test_refresh_token_is_opaque_and_random(self, service, repo):
        repo.get_by_username.return_value = _user()
        _, a = await service.login("testuser", SENHA)
        _, b = await service.login("testuser", SENHA)
        assert a != b
        assert a.count(".") == 0  # not a JWT: it means nothing outside the database
        assert len(a) >= 43

    async def test_new_login_starts_a_new_family(self, service, repo):
        repo.get_by_username.return_value = _user()
        await service.login("testuser", SENHA)
        assert repo.create_refresh_token.await_args.kwargs.get("family_id") is None

    async def test_success_records_last_login(self, service, repo):
        repo.get_by_username.return_value = _user()
        await service.login("testuser", SENHA)
        repo.update_last_login.assert_awaited_once()

    async def test_success_stores_ip_user_agent_and_application(self, service, repo):
        repo.get_by_username.return_value = _user()

        await service.login("testuser", SENHA, ip_address="203.0.113.9", user_agent="curl/8")

        kwargs = repo.create_refresh_token.await_args.kwargs
        assert kwargs["ip_address"] == "203.0.113.9"
        assert kwargs["user_agent"] == "curl/8"
        assert kwargs["application_id"] == TREM_ID

    async def test_propagates_must_change_password(self, service, repo):
        repo.get_by_username.return_value = _user(must_change_password=True)
        result, _ = await service.login("testuser", SENHA)
        assert result.must_change_password is True

    async def test_unknown_user_raises_invalid_credentials(self, service, repo):
        repo.get_by_username.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.login("ninguem", SENHA)

    async def test_unknown_user_still_pays_for_a_password_check(self, service, repo):
        """Sem o hash de mentira, o tempo de resposta revelaria quais usernames existem."""
        repo.get_by_username.return_value = None
        with patch("app.services.auth_service.burn_password_check") as burn:
            with pytest.raises(InvalidCredentialsError):
                await service.login("ninguem", SENHA)
        burn.assert_called_once_with(SENHA)

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

    async def test_access_token_carries_permissions(self, service, repo):
        repo.get_by_username.return_value = _user(permissions=("trem:airline_company", "trem:ai"))
        result, _ = await service.login("testuser", SENHA)

        claims = decode_token(result.access_token)
        assert claims["perms"] == ["trem:ai", "trem:airline_company"]
        assert claims["aud"] == "trem"
        assert claims["amr"] == ["pwd"]


class TestLoginForOtherApplications:
    async def test_token_carries_only_that_applications_permissions(self, service, repo):
        repo.get_by_username.return_value = _user(permissions=("trem:ai", "financas:read"))

        result, _ = await service.login("testuser", SENHA, audience="financas")

        claims = decode_token(result.access_token, audience="financas")
        assert claims["perms"] == ["financas:read"]

    async def test_user_without_grants_in_the_application_is_refused(self, service, repo):
        repo.get_by_username.return_value = _user(permissions=("trem:ai",))
        with pytest.raises(ApplicationAccessDeniedError):
            await service.login("testuser", SENHA, audience="financas")
        repo.create_refresh_token.assert_not_awaited()

    async def test_unknown_application_is_refused(self, service, repo):
        repo.get_by_username.return_value = _user()
        with pytest.raises(ApplicationAccessDeniedError):
            await service.login("testuser", SENHA, audience="inexistente")

    async def test_trem_needs_no_grant(self, service, repo):
        """É na trem que a conta troca a senha e gerencia as chaves."""
        repo.get_by_username.return_value = _user(permissions=())
        result, _ = await service.login("testuser", SENHA)
        assert decode_token(result.access_token)["perms"] == []


class TestLoginThrottling:
    async def test_locks_username_after_repeated_failures(self, service, repo):
        repo.get_by_username.return_value = _user()
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                await service.login("testuser", "errada")

        with pytest.raises(TooManyLoginAttemptsError) as exc:
            await service.login("testuser", SENHA)
        assert exc.value.retry_after > 0

    async def test_unknown_usernames_lock_the_same_way(self, service, repo):
        for _ in range(3):
            with pytest.raises(InvalidCredentialsError):
                await service.login("fantasma", "errada")
        with pytest.raises(TooManyLoginAttemptsError):
            await service.login("fantasma", "errada")

    async def test_username_is_case_insensitive_for_the_counter(self, service, repo):
        for name in ("Fantasma", "FANTASMA", "fantasma"):
            with pytest.raises(InvalidCredentialsError):
                await service.login(name, "errada")
        with pytest.raises(TooManyLoginAttemptsError):
            await service.login("fantasma", "errada")

    async def test_success_resets_the_username_counter(self, service, repo):
        repo.get_by_username.return_value = _user()
        for _ in range(2):
            with pytest.raises(InvalidCredentialsError):
                await service.login("testuser", "errada")
        await service.login("testuser", SENHA)
        for _ in range(2):
            with pytest.raises(InvalidCredentialsError):
                await service.login("testuser", "errada")
        await service.login("testuser", SENHA)

    async def test_ip_is_limited_across_usernames(self, repo, permissions, session):
        throttle = LoginThrottle(per_username=100, per_ip=3, window_seconds=60)
        with patch("app.services.auth_service.UserRepository", return_value=repo), patch(
            "app.services.auth_service.PermissionRepository", return_value=permissions
        ):
            service = AuthService(session, throttle=throttle)
            for i in range(3):
                with pytest.raises(InvalidCredentialsError):
                    await service.login(f"user{i}", "errada", ip_address="198.51.100.7")
            with pytest.raises(TooManyLoginAttemptsError):
                await service.login("outro", "errada", ip_address="198.51.100.7")
            with pytest.raises(InvalidCredentialsError):
                await service.login("outro", "errada", ip_address="198.51.100.8")

    def test_window_expires(self):
        now = [0.0]
        throttle = LoginThrottle(per_username=1, per_ip=10, window_seconds=60, clock=lambda: now[0])
        throttle.record_failure("u", None)
        with pytest.raises(TooManyLoginAttemptsError):
            throttle.check("u", None)
        now[0] = 61
        throttle.check("u", None)


class TestRefresh:
    async def test_success_returns_a_new_pair(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user()

        result, raw_refresh = await service.refresh("raw-old")

        assert result.access_token
        assert raw_refresh and raw_refresh != "raw-old"

    async def test_looks_up_token_by_hash(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user()

        await service.refresh("raw-old")

        repo.get_refresh_token.assert_awaited_once_with(hash_token("raw-old"))

    async def test_rotation_keeps_the_family_and_links_the_successor(self, service, repo):
        stored = _stored_token()
        repo.get_refresh_token.return_value = stored
        repo.get_by_id.return_value = _user()

        await service.refresh("raw-old")

        repo.claim_refresh_token.assert_awaited_once_with(stored.id)
        assert repo.create_refresh_token.await_args.kwargs["family_id"] == FAMILY
        successor = repo.create_refresh_token.return_value.id
        repo.set_refresh_token_successor.assert_awaited_once_with(stored.id, successor)

    async def test_new_access_token_is_for_the_login_application(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token(application="financas")
        repo.get_by_id.return_value = _user(permissions=("financas:read",))

        result, _ = await service.refresh("raw-old")

        assert decode_token(result.access_token, audience="financas")["perms"] == ["financas:read"]

    async def test_unknown_token_raises(self, service, repo):
        repo.get_refresh_token.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("nao-existe")

    async def test_reuse_of_a_rotated_token_revokes_the_family(self, service, repo, session):
        repo.get_refresh_token.return_value = _stored_token(revoked=True, replaced=True)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.revoke_refresh_token_family.assert_awaited_once_with(FAMILY)
        session.commit.assert_awaited()  # must survive the error response's rollback

    async def test_losing_a_concurrent_rotation_counts_as_reuse(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token()
        repo.claim_refresh_token.return_value = False
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.revoke_refresh_token_family.assert_awaited_once_with(FAMILY)
        repo.create_refresh_token.assert_not_awaited()

    async def test_logged_out_token_is_refused_without_alarm(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token(revoked=True)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.revoke_refresh_token_family.assert_not_awaited()

    async def test_expired_stored_token_raises(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token(expired=True)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.create_refresh_token.assert_not_awaited()

    async def test_deleted_user_raises(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = None
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")

    async def test_blocked_user_raises_and_ends_the_login(self, service, repo):
        """Bloquear a conta precisa invalidar a renovação em curso."""
        repo.get_refresh_token.return_value = _stored_token()
        repo.get_by_id.return_value = _user(status=UserStatus.BLOCKED.value)
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.revoke_refresh_token_family.assert_awaited_once_with(FAMILY)

    async def test_losing_every_grant_in_the_application_ends_the_login(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token(application="financas")
        repo.get_by_id.return_value = _user(permissions=("trem:ai",))
        with pytest.raises(InvalidCredentialsError):
            await service.refresh("raw-old")
        repo.revoke_refresh_token_family.assert_awaited_once_with(FAMILY)


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
    async def test_revokes_the_whole_login(self, service, repo):
        repo.get_refresh_token.return_value = _stored_token()
        await service.logout("meu-refresh-token")
        repo.get_refresh_token.assert_awaited_once_with(hash_token("meu-refresh-token"))
        repo.revoke_refresh_token_family.assert_awaited_once_with(FAMILY)

    async def test_unknown_token_does_not_raise(self, service, repo):
        """Logout é idempotente — token desconhecido não deve estourar erro."""
        await service.logout("token-inexistente")
        repo.revoke_refresh_token_family.assert_not_awaited()
