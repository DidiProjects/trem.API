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

from app.domain.entities.profile import ProfileName
from app.domain.entities.user import UserStatus
from app.infrastructure.database.models import (
    Base,
    ProfileModel,
    RefreshTokenModel,
    UserModel,
)


def _user(profile_id, username="alguem", **kwargs):
    params = {
        "username": username,
        "email": f"{username}@example.com",
        "password_hash": "$argon2id$hash",
        "profile_id": uuid.UUID(profile_id) if isinstance(profile_id, str) else profile_id,
    }
    params.update(kwargs)
    return UserModel(**params)


class TestMetadata:
    def test_declares_expected_tables(self):
        assert set(Base.metadata.tables) == {
            "profiles",
            "users",
            "refresh_tokens",
            "api_clients",
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
    async def test_new_user_is_blocked(self, db_session, seeded_profiles):
        user = _user(seeded_profiles[ProfileName.FILE_EDITOR.value])
        db_session.add(user)
        await db_session.flush()
        assert user.status == UserStatus.BLOCKED.value

    async def test_new_user_must_change_password(self, db_session, seeded_profiles):
        user = _user(seeded_profiles[ProfileName.FILE_EDITOR.value])
        db_session.add(user)
        await db_session.flush()
        assert user.must_change_password is True

    async def test_id_is_generated(self, db_session, seeded_profiles):
        user = _user(seeded_profiles[ProfileName.FILE_EDITOR.value])
        db_session.add(user)
        await db_session.flush()
        assert isinstance(user.id, uuid.UUID)

    async def test_timestamps_are_populated(self, db_session, seeded_profiles):
        user = _user(seeded_profiles[ProfileName.FILE_EDITOR.value])
        db_session.add(user)
        await db_session.flush()
        assert user.created_at is not None
        assert user.updated_at is not None

    async def test_optional_fields_start_null(self, db_session, seeded_profiles):
        user = _user(seeded_profiles[ProfileName.FILE_EDITOR.value])
        db_session.add(user)
        await db_session.flush()
        assert user.last_login_at is None
        assert user.provisional_password_sent_at is None


class TestUserConstraints:
    async def test_username_is_unique(self, db_session, seeded_profiles):
        pid = seeded_profiles[ProfileName.FILE_EDITOR.value]
        db_session.add(_user(pid, "duplicado", email="a@example.com"))
        await db_session.flush()

        db_session.add(_user(pid, "duplicado", email="b@example.com"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_email_is_unique(self, db_session, seeded_profiles):
        pid = seeded_profiles[ProfileName.FILE_EDITOR.value]
        db_session.add(_user(pid, "user-a", email="mesmo@example.com"))
        await db_session.flush()

        db_session.add(_user(pid, "user-b", email="mesmo@example.com"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_multiple_users_may_have_null_email(self, db_session, seeded_profiles):
        pid = seeded_profiles[ProfileName.FILE_EDITOR.value]
        db_session.add(_user(pid, "user-a", email=None))
        db_session.add(_user(pid, "user-b", email=None))
        await db_session.flush()  # NULL não colide em índice único

    @pytest.mark.parametrize(
        "status", [UserStatus.ACTIVE, UserStatus.BLOCKED, UserStatus.SUSPENDED]
    )
    async def test_valid_status_is_accepted(self, db_session, seeded_profiles, status):
        db_session.add(
            _user(
                seeded_profiles[ProfileName.FILE_EDITOR.value],
                f"user-{status.value}",
                status=status.value,
            )
        )
        await db_session.flush()

    async def test_invalid_status_is_rejected_by_check_constraint(
        self, db_session, seeded_profiles
    ):
        """A regra de status vive no banco, não só na aplicação."""
        db_session.add(
            _user(
                seeded_profiles[ProfileName.FILE_EDITOR.value],
                "invalido",
                status="superadmin",
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_password_hash_is_required(self, db_session, seeded_profiles):
        user = UserModel(
            username="sem-hash",
            profile_id=uuid.UUID(seeded_profiles[ProfileName.FILE_EDITOR.value]),
        )
        db_session.add(user)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_unknown_profile_is_rejected(self, db_session, seeded_profiles):
        db_session.add(_user(uuid.uuid4(), "orfao"))
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestProfileConstraints:
    async def test_name_is_unique(self, db_session, seeded_profiles):
        db_session.add(ProfileModel(name=ProfileName.FILE_EDITOR.value))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_unknown_profile_name_is_rejected(self, db_session, seeded_profiles):
        db_session.add(ProfileModel(name="administrador"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_seed_creates_both_profiles(self, db_session, seeded_profiles):
        result = await db_session.execute(select(ProfileModel.name))
        assert set(result.scalars().all()) == {
            ProfileName.FILE_EDITOR.value,
            ProfileName.AIRLINE_COMPANY.value,
        }


class TestRefreshTokenModel:
    def _token(self, user, token_hash="hash-x", **kwargs):
        params = {
            "user_id": user.id,
            "token_hash": token_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
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
    async def test_user_exposes_profile(self, db_session, persisted_user):
        assert persisted_user.profile.name == ProfileName.FILE_EDITOR.value

    async def test_profile_exposes_users(self, db_session, persisted_user):
        profile = await db_session.get(
            ProfileModel, persisted_user.profile_id, options=[]
        )
        await db_session.refresh(profile, ["users"])
        assert persisted_user.username in {u.username for u in profile.users}


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

    async def test_profile_repr_includes_name(self, db_session, persisted_user):
        assert ProfileName.FILE_EDITOR.value in repr(persisted_user.profile)
