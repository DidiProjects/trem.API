"""
Testes de UserService — criação de conta e envio de senha provisória.

Repositório e cliente de email são mockados; a geração/hash da senha é real.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AppException, UserAlreadyExistsError, UserNotFoundError
from app.core.security import verify_password
from app.domain.entities.profile import ProfileName
from app.domain.schemas.user import UserCreate
from app.services.user_service import UserService
from tests.conftest import make_mock_user

PROFILE_ID = "00000000-0000-0000-0000-000000000010"


@pytest.fixture
def repo():
    mock = MagicMock()
    mock.get_by_id = AsyncMock(return_value=None)
    mock.get_by_username = AsyncMock(return_value=None)
    mock.get_profile_by_name = AsyncMock(
        return_value=MagicMock(id=PROFILE_ID, name=ProfileName.FILE_EDITOR.value)
    )
    mock.create = AsyncMock(return_value=make_mock_user())
    mock.update_password = AsyncMock()
    mock.set_provisional_password_sent = AsyncMock()
    mock.list_all = AsyncMock(return_value=[])
    mock.count_all = AsyncMock(return_value=0)
    return mock


@pytest.fixture
def email():
    mock = MagicMock()
    mock.send_provisional_password = AsyncMock()
    return mock


@pytest.fixture
def service(repo, email):
    with patch("app.services.user_service.UserRepository", return_value=repo), patch(
        "app.services.user_service.SmtpEmailClient", return_value=email
    ):
        yield UserService(MagicMock())


class TestCreateUser:
    async def test_success_returns_user(self, service, repo):
        data = UserCreate(
            username="novo", email="novo@example.com", profile_name="file_editor"
        )
        user = await service.create_user(data)
        assert user is not None
        repo.create.assert_awaited_once()

    async def test_resolves_profile_id_from_name(self, service, repo):
        data = UserCreate(username="novo", profile_name="file_editor")
        await service.create_user(data)
        assert repo.create.await_args.kwargs["profile_id"] == PROFILE_ID

    async def test_stores_unusable_placeholder_password(self, service, repo):
        """
        A conta nasce sem senha real: o hash gravado não pode validar nenhuma
        senha que o usuário conheça, até o admin enviar a provisória.
        """
        data = UserCreate(username="novo", profile_name="file_editor")
        await service.create_user(data)

        placeholder = repo.create.await_args.kwargs["password_hash"]
        assert placeholder.startswith("$argon2id$")
        assert verify_password("", placeholder) is False
        assert verify_password("novo", placeholder) is False

    async def test_duplicate_username_raises(self, service, repo):
        repo.get_by_username.return_value = make_mock_user()
        data = UserCreate(username="existente", profile_name="file_editor")

        with pytest.raises(UserAlreadyExistsError):
            await service.create_user(data)

    async def test_duplicate_username_does_not_create(self, service, repo):
        repo.get_by_username.return_value = make_mock_user()
        data = UserCreate(username="existente", profile_name="file_editor")

        with pytest.raises(UserAlreadyExistsError):
            await service.create_user(data)
        repo.create.assert_not_awaited()

    async def test_missing_profile_in_database_raises(self, service, repo):
        """Perfil válido no schema mas ausente no banco (seed não rodou)."""
        repo.get_profile_by_name.return_value = None
        data = UserCreate(username="novo", profile_name="file_editor")

        with pytest.raises(AppException) as exc:
            await service.create_user(data)
        assert exc.value.status_code == 400

    async def test_passes_email_through(self, service, repo):
        data = UserCreate(
            username="novo", email="Novo@Example.COM", profile_name="file_editor"
        )
        await service.create_user(data)
        assert repo.create.await_args.kwargs["email"] == "novo@example.com"


class TestSendProvisionalPassword:
    async def test_success_sends_email(self, service, repo, email):
        repo.get_by_id.return_value = make_mock_user()
        await service.send_provisional_password("id-1")
        email.send_provisional_password.assert_awaited_once()

    async def test_success_sets_must_change_password(self, service, repo):
        repo.get_by_id.return_value = make_mock_user()
        await service.send_provisional_password("id-1")
        assert repo.update_password.await_args.kwargs["must_change_password"] is True

    async def test_emailed_password_matches_stored_hash(self, service, repo, email):
        """A senha enviada por email tem que ser a que ficou gravada."""
        repo.get_by_id.return_value = make_mock_user()
        await service.send_provisional_password("id-1")

        enviada = email.send_provisional_password.await_args.kwargs["provisional_password"]
        gravada = repo.update_password.await_args.kwargs["password_hash"]
        assert verify_password(enviada, gravada) is True

    async def test_records_sent_timestamp(self, service, repo):
        repo.get_by_id.return_value = make_mock_user()
        await service.send_provisional_password("id-1")
        repo.set_provisional_password_sent.assert_awaited_once()

    async def test_generates_a_different_password_each_time(self, service, repo, email):
        repo.get_by_id.return_value = make_mock_user()
        await service.send_provisional_password("id-1")
        primeira = email.send_provisional_password.await_args.kwargs["provisional_password"]

        await service.send_provisional_password("id-1")
        segunda = email.send_provisional_password.await_args.kwargs["provisional_password"]

        assert primeira != segunda

    async def test_unknown_user_raises(self, service, repo):
        repo.get_by_id.return_value = None
        with pytest.raises(UserNotFoundError):
            await service.send_provisional_password("id-1")

    async def test_user_without_email_raises_422(self, service, repo):
        user = make_mock_user()
        user.email = None
        repo.get_by_id.return_value = user

        with pytest.raises(AppException) as exc:
            await service.send_provisional_password("id-1")
        assert exc.value.status_code == 422

    async def test_user_without_email_does_not_change_password(self, service, repo):
        user = make_mock_user()
        user.email = None
        repo.get_by_id.return_value = user

        with pytest.raises(AppException):
            await service.send_provisional_password("id-1")
        repo.update_password.assert_not_awaited()


class TestGetUser:
    async def test_success(self, service, repo):
        repo.get_by_id.return_value = make_mock_user()
        assert (await service.get_user("id-1")).username == "testuser"

    async def test_unknown_user_raises(self, service, repo):
        repo.get_by_id.return_value = None
        with pytest.raises(UserNotFoundError):
            await service.get_user("id-1")


class TestListUsers:
    async def test_returns_page_and_total(self, service, repo):
        repo.list_all.return_value = [make_mock_user()]
        repo.count_all.return_value = 42

        users, total = await service.list_users(limit=1, offset=0)

        assert len(users) == 1
        assert total == 42

    async def test_forwards_pagination_to_repository(self, service, repo):
        await service.list_users(limit=25, offset=50)
        repo.list_all.assert_awaited_once_with(limit=25, offset=50)

    async def test_empty_table(self, service, repo):
        users, total = await service.list_users()
        assert users == []
        assert total == 0
