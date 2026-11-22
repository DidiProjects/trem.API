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
    TokenExpiredError,
    TokenInvalidError,
)
from app.domain.entities.permission import Permission, TremPermission, split_permission_key
from app.domain.schemas.common import ErrorResponse, MessageResponse, PaginatedResponse
from tests.conftest import make_mock_user


# ---------------------------------------------------------------------------
# Enums e entidades
# ---------------------------------------------------------------------------

class TestTremPermissionEnum:
    def test_compares_equal_to_plain_string(self):
        assert TremPermission.FILE_EDITOR == "trem:file_editor"
        assert TremPermission.AIRLINE_COMPANY == "trem:airline_company"

    def test_every_key_is_well_formed(self):
        for key in TremPermission:
            assert split_permission_key(key)[0] == "trem"


class TestPermissionKey:
    @pytest.mark.parametrize("key,expected", [
        ("trem:ai", ("trem", "ai")),
        ("financas:read.all", ("financas", "read.all")),
        ("my-app:file_editor", ("my-app", "file_editor")),
    ])
    def test_splits_application_and_name(self, key, expected):
        assert split_permission_key(key) == expected

    @pytest.mark.parametrize("key", ["ai", "trem:", ":ai", "Trem:ai", "trem:AI", "t:ai", "trem:a i", "a:b:c d"])
    def test_rejects_malformed_keys(self, key):
        with pytest.raises(ValueError):
            split_permission_key(key)


class TestUserEntity:
    def test_has_permission(self):
        user = make_mock_user((TremPermission.FILE_EDITOR.value, TremPermission.AI.value))
        assert user.has_permission("trem:file_editor") is True
        assert user.has_permission("trem:ai") is True
        assert user.has_permission("trem:airline_company") is False

    def test_is_admin_follows_trem_admin(self):
        assert make_mock_user(("trem:admin",)).is_admin is True
        assert make_mock_user().is_admin is False


class TestPermissionEntity:
    def test_key_joins_application_and_name(self):
        permission = Permission(
            id="id-1", application="trem", name="file_editor", description="desc",
            created_at=datetime.now(timezone.utc),
        )
        assert permission.key == "trem:file_editor"


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
            (TokenExpiredError(), 401),
            (TokenInvalidError(), 401),
            (InsufficientPermissionsError(), 403),
            (EmailSendError(), 503),
        ],
    )
    def test_status_codes(self, exc, status):
        assert exc.status_code == status

    def test_all_inherit_app_exception(self):
        assert isinstance(TokenInvalidError(), AppException)

    def test_app_exception_defaults_to_500(self):
        assert AppException("falhou").status_code == 500

    def test_email_send_error_appends_detail(self):
        assert "SMTP fora do ar" in EmailSendError("SMTP fora do ar").message

    def test_message_is_the_str_representation(self):
        assert str(TokenInvalidError()) == TokenInvalidError().message
