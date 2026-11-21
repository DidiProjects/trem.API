"""
Testes dos modelos ORM contra o SQLite da suíte.

Cobrem o que é fácil de quebrar numa migration: defaults, unicidade,
CHECK constraints, cascade e os índices declarados.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.domain.entities.permission import TremPermission
from app.domain.entities.user import UserStatus
from app.infrastructure.database.models import (
    Base,
    ApplicationModel,
    PermissionModel,
    RefreshTokenModel,
    UserModel,
    user_permissions,
)


def _user(username="alguem", **kwargs):
    params = {
        "username": username,
        "email": f"{username}@example.com",
        "password_hash": "$argon2id$hash",
    }
    params.update(kwargs)
    return UserModel(**params)


class TestMetadata:
    def test_declares_expected_tables(self):
        assert set(Base.metadata.tables) == {
            "applications",
            "email_tokens",
            "mfa_recovery_codes",
            "permissions",
            "users",
            "refresh_tokens",
            "api_clients",
            "user_permissions",
            "api_client_permissions",
            "hub_services",
            "oidc_clients",
            "oidc_grants",
        }

    def test_refresh_token_indexes_are_declared(self):
        indexes = {i.name for i in Base.metadata.tables["refresh_tokens"].indexes}
        assert "ix_refresh_tokens_user_id_revoked" in indexes
        assert "ix_refresh_tokens_expires_at" in indexes

    def test_user_lookup_columns_are_indexed(self):
        indexed = {
            col.name
            for index in Base.metadata.tables["users"].indexes
            for col in index.columns
        }
        assert {"username", "status"} <= indexed


class TestUserDefaults:
    async def test_new_user_is_blocked(self, db_session, seeded_permissions):
        user = _user()
        db_session.add(user)
        await db_session.flush()
        assert user.status == UserStatus.BLOCKED.value

    async def test_new_user_must_change_password(self, db_session, seeded_permissions):
        user = _user()
        db_session.add(user)
        await db_session.flush()
        assert user.must_change_password is True

    async def test_id_is_generated(self, db_session, seeded_permissions):
        user = _user()
        db_session.add(user)
        await db_session.flush()
        assert isinstance(user.id, uuid.UUID)

    async def test_timestamps_are_populated(self, db_session, seeded_permissions):
        user = _user()
        db_session.add(user)
        await db_session.flush()
        assert user.created_at is not None
        assert user.updated_at is not None

    async def test_optional_fields_start_null(self, db_session, seeded_permissions):
        user = _user()
        db_session.add(user)
        await db_session.flush()
        assert user.last_login_at is None
        assert user.provisional_password_sent_at is None


class TestUserConstraints:
    async def test_username_is_unique(self, db_session, seeded_permissions):
        db_session.add(_user("duplicado", email="a@example.com"))
        await db_session.flush()

        db_session.add(_user("duplicado", email="b@example.com"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_email_is_unique(self, db_session, seeded_permissions):
        db_session.add(_user("user-a", email="mesmo@example.com"))
        await db_session.flush()

        db_session.add(_user("user-b", email="mesmo@example.com"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_multiple_users_may_have_null_email(self, db_session, seeded_permissions):
        db_session.add(_user("user-a", email=None))
        db_session.add(_user("user-b", email=None))
        await db_session.flush()  # NULL não colide em índice único

    @pytest.mark.parametrize(
        "status", [UserStatus.ACTIVE, UserStatus.BLOCKED, UserStatus.SUSPENDED]
    )
    async def test_valid_status_is_accepted(self, db_session, seeded_permissions, status):
        db_session.add(
            _user(
                f"user-{status.value}",
                status=status.value,
            )
        )
        await db_session.flush()

    async def test_invalid_status_is_rejected_by_check_constraint(
        self, db_session, seeded_permissions
    ):
        """A regra de status vive no banco, não só na aplicação."""
        db_session.add(
            _user(
                "invalido",
                status="superadmin",
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_password_hash_is_required(self, db_session, seeded_permissions):
        user = UserModel(username="sem-hash")
        db_session.add(user)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_unknown_permission_is_rejected(self, db_session, seeded_permissions):
        user = _user("orfao")
        db_session.add(user)
        await db_session.flush()
        with pytest.raises(IntegrityError):
            await db_session.execute(
                user_permissions.insert().values(user_id=user.id, permission_id=uuid.uuid4())
            )

    async def test_user_may_hold_several_permissions(self, db_session, seeded_permissions):
        keys = [TremPermission.FILE_EDITOR.value, TremPermission.AI.value]
        permissions = [await db_session.get(PermissionModel, uuid.UUID(seeded_permissions[k])) for k in keys]
        user = _user("varios", permissions=permissions)
        db_session.add(user)
        await db_session.flush()
        assert {p.key for p in user.permissions} == set(keys)

    async def test_grant_records_when(self, db_session, seeded_permissions):
        user = _user("auditado")
        db_session.add(user)
        await db_session.flush()
        pid = uuid.UUID(seeded_permissions[TremPermission.AI.value])
        await db_session.execute(user_permissions.insert().values(user_id=user.id, permission_id=pid))
        row = (await db_session.execute(select(user_permissions))).one()
        assert row.granted_at is not None and row.granted_by is None

    async def test_same_permission_twice_is_rejected(self, db_session, seeded_permissions):
        user = _user("repetido")
        db_session.add(user)
        await db_session.flush()
        pid = uuid.UUID(seeded_permissions[TremPermission.AI.value])
        await db_session.execute(user_permissions.insert().values(user_id=user.id, permission_id=pid))
        with pytest.raises(IntegrityError):
            await db_session.execute(user_permissions.insert().values(user_id=user.id, permission_id=pid))


class TestPermissionConstraints:
    async def _trem(self, db_session):
        return (await db_session.execute(select(ApplicationModel))).scalar_one()

    async def test_name_is_unique_within_application(self, db_session, seeded_permissions):
        db_session.add(PermissionModel(application=await self._trem(db_session), name="ai"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_same_name_in_another_application_is_a_different_permission(
        self, db_session, seeded_permissions
    ):
        other = ApplicationModel(slug="financas", name="Finanças")
        db_session.add(PermissionModel(application=other, name="ai"))
        await db_session.flush()
        keys = {p.key for p in (await db_session.execute(select(PermissionModel))).scalars()}
        assert {"trem:ai", "financas:ai"} <= keys

    async def test_permission_requires_application(self, db_session, seeded_permissions):
        db_session.add(PermissionModel(name="solta"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_application_slug_is_unique(self, db_session, seeded_permissions):
        db_session.add(ApplicationModel(slug="trem", name="outra"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_deleting_application_deletes_its_permissions_and_grants(
        self, db_session, persisted_user
    ):
        await db_session.delete(await self._trem(db_session))
        await db_session.flush()
        assert (await db_session.execute(select(PermissionModel))).scalars().all() == []
        assert (await db_session.execute(select(user_permissions))).all() == []

    async def test_seed_creates_every_trem_permission(self, db_session, seeded_permissions):
        result = await db_session.execute(select(PermissionModel))
        assert {p.key for p in result.scalars()} == {p.value for p in TremPermission}


class TestRefreshTokenModel:
    def _token(self, user, token_hash="hash-x", **kwargs):
        params = {
            "user_id": user.id,
            "token_hash": token_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
            "family_id": uuid.uuid4(),
            "application_id": user.permissions[0].application_id,
        }
        params.update(kwargs)
        return RefreshTokenModel(**params)

    async def test_defaults_to_not_revoked(self, db_session, persisted_user):
        token = self._token(persisted_user)
        db_session.add(token)
        await db_session.flush()
        assert token.revoked is False

    async def test_token_hash_is_unique(self, db_session, persisted_user):
        db_session.add(self._token(persisted_user, "mesmo-hash"))
        await db_session.flush()

        db_session.add(self._token(persisted_user, "mesmo-hash"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_deleting_user_cascades_to_tokens(self, db_session, persisted_user):
        db_session.add(self._token(persisted_user))
        await db_session.flush()

        await db_session.delete(persisted_user)
        await db_session.flush()

        result = await db_session.execute(select(RefreshTokenModel))
        assert result.scalars().all() == []

    async def test_stores_ipv6_address(self, db_session, persisted_user):
        """A coluna precisa comportar IPv6 (45 chars no fallback não-PG)."""
        token = self._token(
            persisted_user, ip_address="2001:0db8:85a3:0000:0000:8a2e:0370:7334"
        )
        db_session.add(token)
        await db_session.flush()
        assert token.ip_address.startswith("2001:")


class TestRelationships:
    async def test_user_exposes_permissions(self, db_session, persisted_user):
        assert [p.key for p in persisted_user.permissions] == [TremPermission.FILE_EDITOR.value]

    async def test_permission_exposes_application(self, db_session, persisted_user):
        assert persisted_user.permissions[0].application.slug == "trem"


class TestRepr:
    async def test_user_repr_hides_password_hash(self, db_session, persisted_user):
        """__repr__ aparece em traceback e log — não pode vazar o hash."""
        assert "$argon2id$" not in repr(persisted_user)
        assert persisted_user.username in repr(persisted_user)

    async def test_refresh_token_repr_hides_token_hash(self, db_session, persisted_user):
        token = RefreshTokenModel(
            user_id=persisted_user.id,
            token_hash="segredo-absoluto",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        assert "segredo-absoluto" not in repr(token)

    async def test_permission_repr_includes_name(self, db_session, persisted_user):
        assert "file_editor" in repr(persisted_user.permissions[0])
