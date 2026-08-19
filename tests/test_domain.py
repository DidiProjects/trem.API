"""
Testes das entidades de domínio, dos enums e dos schemas Pydantic.

São as regras que rodam antes de qualquer I/O — validação de entrada,
serialização de saída e a semântica de status/perfil.
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core.exceptions import (
    AppException,
    EmailSendError,
    InsufficientPermissionsError,
    InvalidCredentialsError,
    PasswordChangeRequiredError,
    TokenExpiredError,
    TokenInvalidError,
    UserAlreadyExistsError,
    UserBlockedError,
    UserNotFoundError,
)
from app.domain.entities.profile import AIRLINE_COMPANY, FILE_EDITOR, Profile, ProfileName
from app.domain.entities.user import UserStatus
from app.domain.schemas.auth import (
    AccessTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.domain.schemas.common import ErrorResponse, MessageResponse, PaginatedResponse
from app.domain.schemas.user import UserCreate, UserResponse
from tests.conftest import make_mock_user


# ---------------------------------------------------------------------------
# Enums e entidades
# ---------------------------------------------------------------------------

class TestUserStatusEnum:
    def test_compares_equal_to_plain_string(self):
        """StrEnum: código antigo que compara com string precisa continuar valendo."""
        assert UserStatus.ACTIVE == "active"
        assert UserStatus.BLOCKED == "blocked"
        assert UserStatus.SUSPENDED == "suspended"

    def test_has_exactly_three_states(self):
        assert {s.value for s in UserStatus} == {"active", "blocked", "suspended"}


class TestProfileNameEnum:
    def test_compares_equal_to_plain_string(self):
        assert ProfileName.FILE_EDITOR == "file_editor"
        assert ProfileName.AIRLINE_COMPANY == "airline_company"

    def test_legacy_constants_still_resolve(self):
        assert FILE_EDITOR == "file_editor"
        assert AIRLINE_COMPANY == "airline_company"


class TestUserEntity:
    def test_active_user_flags(self):
        user = make_mock_user(status=UserStatus.ACTIVE.value)
        assert user.is_active is True
        assert user.is_blocked is False
        assert user.is_suspended is False
        assert user.can_authenticate is True

    def test_blocked_user_flags(self):
        user = make_mock_user(status=UserStatus.BLOCKED.value)
        assert user.is_blocked is True
        assert user.is_active is False
        assert user.can_authenticate is False

    def test_suspended_user_flags(self):
        user = make_mock_user(status=UserStatus.SUSPENDED.value)
        assert user.is_suspended is True
        assert user.is_active is False
        assert user.can_authenticate is False

    def test_has_profile(self):
        user = make_mock_user(profile_name=ProfileName.FILE_EDITOR.value)
        assert user.has_profile("file_editor") is True
        assert user.has_profile("airline_company") is False

    def test_repr_hides_password_hash(self):
        assert "$argon2id$" not in repr(make_mock_user())

    def test_repr_shows_identity(self):
        text = repr(make_mock_user())
        assert "testuser" in text
        assert "file_editor" in text


class TestProfileEntity:
    def test_holds_fields(self):
        profile = Profile(
            id="id-1", name="file_editor", description="desc", created_at=datetime.now(timezone.utc)
        )
        assert profile.name == "file_editor"


# ---------------------------------------------------------------------------
# Schemas de autenticação
# ---------------------------------------------------------------------------

class TestLoginRequest:
    def test_valid_payload(self):
        body = LoginRequest(username="joao", password="Senha@123")
        assert body.username == "joao"

    def test_strips_surrounding_whitespace(self):
        assert LoginRequest(username="  joao  ", password="Senha@123").username == "joao"

    @pytest.mark.parametrize("username", ["", "   "])
    def test_rejects_blank_username(self, username):
        with pytest.raises(ValidationError):
            LoginRequest(username=username, password="Senha@123")

    @pytest.mark.parametrize("password", ["", "   "])
    def test_rejects_blank_password(self, password):
        with pytest.raises(ValidationError):
            LoginRequest(username="joao", password=password)

    def test_rejects_missing_fields(self):
        with pytest.raises(ValidationError):
            LoginRequest(username="joao")

    def test_rejects_absurdly_long_password(self):
        """Limite de tamanho evita DoS no Argon2id."""
        with pytest.raises(ValidationError):
            LoginRequest(username="joao", password="x" * 129)


class TestChangePasswordRequest:
    def test_valid_payload(self):
        body = ChangePasswordRequest(
            current_password="Antiga@123", new_password="NovaSenha@456"
        )
        assert body.new_password == "NovaSenha@456"

    @pytest.mark.parametrize(
        "senha,motivo",
        [
            ("Curta1", "menos de 8 caracteres"),
            ("semmaiuscula123", "sem maiúscula"),
            ("SEMMINUSCULA123", "sem minúscula"),
            ("SemNumeroAlgum", "sem dígito"),
        ],
    )
    def test_rejects_weak_password(self, senha, motivo):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="Antiga@123", new_password=senha)

    def test_accepts_password_at_minimum_length(self):
        body = ChangePasswordRequest(current_password="Antiga@123", new_password="Senha123")
        assert len(body.new_password) == 8

    def test_rejects_new_password_equal_to_current(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(
                current_password="MesmaSenha1", new_password="MesmaSenha1"
            )

    def test_rejects_blank_current_password(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(current_password="", new_password="NovaSenha@456")


class TestRefreshRequest:
    def test_valid_payload(self):
        assert RefreshRequest(refresh_token="abc.def.ghi").refresh_token == "abc.def.ghi"

    def test_rejects_blank_token(self):
        with pytest.raises(ValidationError):
            RefreshRequest(refresh_token="   ")


class TestTokenResponses:
    def test_token_response_defaults_to_bearer(self):
        body = TokenResponse(
            access_token="a", refresh_token="r", must_change_password=False
        )
        assert body.token_type == "bearer"

    def test_access_token_response_defaults_to_bearer(self):
        assert AccessTokenResponse(access_token="a").token_type == "bearer"


# ---------------------------------------------------------------------------
# Schemas de usuário
# ---------------------------------------------------------------------------

class TestUserCreate:
    def test_valid_payload(self):
        body = UserCreate(
            username="joao.silva", email="joao@example.com", profile_name="file_editor"
        )
        assert body.username == "joao.silva"

    def test_email_is_optional(self):
        assert UserCreate(username="joao", profile_name="file_editor").email is None

    def test_email_is_lowercased(self):
        body = UserCreate(
            username="joao", email="Joao@Example.COM", profile_name="file_editor"
        )
        assert body.email == "joao@example.com"

    @pytest.mark.parametrize("email", ["nao-e-email", "sem@dominio", "@example.com", "a b@c.com"])
    def test_rejects_invalid_email(self, email):
        with pytest.raises(ValidationError):
            UserCreate(username="joao", email=email, profile_name="file_editor")

    @pytest.mark.parametrize("username", ["ab", "x" * 51])
    def test_rejects_username_out_of_length_bounds(self, username):
        with pytest.raises(ValidationError):
            UserCreate(username=username, profile_name="file_editor")

    @pytest.mark.parametrize("username", ["joao silva", "joão", "joao@silva", "joao/silva"])
    def test_rejects_username_with_forbidden_characters(self, username):
        with pytest.raises(ValidationError):
            UserCreate(username=username, profile_name="file_editor")

    @pytest.mark.parametrize("username", ["abc", "joao_silva", "joao.silva", "joao-silva", "user123"])
    def test_accepts_valid_username_shapes(self, username):
        assert UserCreate(username=username, profile_name="file_editor").username == username

    @pytest.mark.parametrize("profile", ["file_editor", "airline_company"])
    def test_accepts_known_profiles(self, profile):
        assert UserCreate(username="joao", profile_name=profile).profile_name == profile

    @pytest.mark.parametrize("profile", ["admin", "superuser", "", "FILE_EDITOR"])
    def test_rejects_unknown_profile(self, profile):
        with pytest.raises(ValidationError):
            UserCreate(username="joao", profile_name=profile)


class TestUserResponse:
    def test_builds_from_domain_entity(self):
        """from_attributes=True permite montar a resposta direto da entidade."""
        response = UserResponse.model_validate(make_mock_user())
        assert response.username == "testuser"
        assert response.status == UserStatus.ACTIVE

    def test_never_exposes_password_hash(self):
        dumped = UserResponse.model_validate(make_mock_user()).model_dump()
        assert "password_hash" not in dumped

    def test_serializes_status_as_string(self):
        dumped = UserResponse.model_validate(make_mock_user()).model_dump(mode="json")
        assert dumped["status"] == "active"


# ---------------------------------------------------------------------------
# Schemas comuns
# ---------------------------------------------------------------------------

class TestPaginatedResponse:
    def test_has_more_when_records_remain(self):
        page = PaginatedResponse[int](items=[1, 2], total=10, limit=2, offset=0)
        assert page.has_more is True

    def test_has_more_false_on_last_page(self):
        page = PaginatedResponse[int](items=[9, 10], total=10, limit=2, offset=8)
        assert page.has_more is False

    def test_has_more_false_when_empty(self):
        page = PaginatedResponse[int](items=[], total=0, limit=10, offset=0)
        assert page.has_more is False

    def test_has_more_is_serialized(self):
        page = PaginatedResponse[int](items=[1], total=5, limit=1, offset=0)
        assert page.model_dump()["has_more"] is True


class TestSimpleSchemas:
    def test_message_response(self):
        assert MessageResponse(message="ok").message == "ok"

    def test_error_response_code_is_optional(self):
        assert ErrorResponse(detail="falhou").code is None


# ---------------------------------------------------------------------------
# Exceções de aplicação
# ---------------------------------------------------------------------------

class TestExceptions:
    @pytest.mark.parametrize(
        "exc,status",
        [
            (InvalidCredentialsError(), 401),
            (TokenExpiredError(), 401),
            (TokenInvalidError(), 401),
            (UserBlockedError(), 403),
            (PasswordChangeRequiredError(), 403),
            (InsufficientPermissionsError(), 403),
            (UserNotFoundError(), 404),
            (UserAlreadyExistsError(), 409),
            (EmailSendError(), 503),
        ],
    )
    def test_status_codes(self, exc, status):
        assert exc.status_code == status

    def test_all_inherit_app_exception(self):
        assert isinstance(UserNotFoundError(), AppException)

    def test_app_exception_defaults_to_500(self):
        assert AppException("falhou").status_code == 500

    def test_invalid_credentials_accepts_custom_message(self):
        assert InvalidCredentialsError("Senha atual incorreta").message == "Senha atual incorreta"

    def test_email_send_error_appends_detail(self):
        assert "SMTP fora do ar" in EmailSendError("SMTP fora do ar").message

    def test_message_is_the_str_representation(self):
        assert str(UserNotFoundError()) == UserNotFoundError().message
