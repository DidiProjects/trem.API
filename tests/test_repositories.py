"""
Testes de UserRepository contra um banco real (SQLite in-memory).

Diferente do resto da suíte, aqui nada é mockado: exercita o SQL de verdade,
os defaults dos modelos e o mapeamento ORM -> entidade de domínio.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.entities.profile import ProfileName
from app.domain.entities.user import User, UserStatus
from app.repositories.user_repository import UserRepository, _as_uuid


@pytest.fixture
def repo(db_session):
    return UserRepository(db_session)


async def _make_user(repo, seeded_profiles, username="novo", email="novo@example.com"):
    return await repo.create(
        username=username,
        password_hash="$argon2id$hash",
        profile_id=seeded_profiles[ProfileName.FILE_EDITOR.value],
        email=email,
    )


class TestAsUuidHelper:
    def test_accepts_uuid_string(self):
        value = "11111111-1111-1111-1111-111111111111"
        assert _as_uuid(value) == uuid.UUID(value)

    def test_passes_through_uuid_object(self):
        value = uuid.uuid4()
        assert _as_uuid(value) is value

    def test_returns_none_for_garbage(self):
        assert _as_uuid("nao-e-uuid") is None

    def test_returns_none_for_none(self):
        assert _as_uuid(None) is None


class TestCreate:
    async def test_returns_domain_entity(self, repo, seeded_profiles):
        user = await _make_user(repo, seeded_profiles)
        assert isinstance(user, User)
        assert user.username == "novo"
        assert user.email == "novo@example.com"

    async def test_resolves_profile_name(self, repo, seeded_profiles):
        user = await _make_user(repo, seeded_profiles)
        assert user.profile_name == ProfileName.FILE_EDITOR.value

    async def test_defaults_to_blocked_and_must_change_password(self, repo, seeded_profiles):
        """Conta nasce bloqueada — o admin habilita depois."""
        user = await _make_user(repo, seeded_profiles)
        assert user.status == UserStatus.BLOCKED
        assert user.must_change_password is True
        assert user.is_active is False

    async def test_generates_id_and_timestamps(self, repo, seeded_profiles):
        user = await _make_user(repo, seeded_profiles)
        assert uuid.UUID(user.id)
        assert user.created_at is not None
        assert user.updated_at is not None
        assert user.last_login_at is None

    async def test_email_is_optional(self, repo, seeded_profiles):
        user = await repo.create(
            username="sememail",
            password_hash="$argon2id$hash",
            profile_id=seeded_profiles[ProfileName.FILE_EDITOR.value],
        )
        assert user.email is None

    async def test_accepts_airline_profile(self, repo, seeded_profiles):
        user = await repo.create(
            username="cia",
            password_hash="$argon2id$hash",
            profile_id=seeded_profiles[ProfileName.AIRLINE_COMPANY.value],
        )
        assert user.profile_name == ProfileName.AIRLINE_COMPANY.value


class TestGetById:
    async def test_finds_existing_user(self, repo, persisted_user):
        found = await repo.get_by_id(str(persisted_user.id))
        assert found is not None
        assert found.username == "persisted"

    async def test_returns_none_for_unknown_id(self, repo, seeded_profiles):
        assert await repo.get_by_id(str(uuid.uuid4())) is None

    async def test_malformed_id_returns_none_instead_of_raising(self, repo, seeded_profiles):
        """Id inválido na URL deve virar 404, não erro de driver."""
        assert await repo.get_by_id("nao-e-uuid") is None

    async def test_loads_profile_relationship(self, repo, persisted_user):
        found = await repo.get_by_id(str(persisted_user.id))
        assert found.profile_name == ProfileName.FILE_EDITOR.value


class TestGetByUsername:
    async def test_finds_existing_user(self, repo, persisted_user):
        found = await repo.get_by_username("persisted")
        assert found is not None
        assert found.id == str(persisted_user.id)

    async def test_returns_none_for_unknown_username(self, repo, seeded_profiles):
        assert await repo.get_by_username("ninguem") is None

    async def test_is_case_sensitive(self, repo, persisted_user):
        assert await repo.get_by_username("PERSISTED") is None


class TestUpdatePassword:
    async def test_changes_hash_and_clears_flag(self, repo, persisted_user):
        await repo.update_password(str(persisted_user.id), "$argon2id$novo", False)
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.password_hash == "$argon2id$novo"
        assert updated.must_change_password is False

    async def test_can_set_must_change_password(self, repo, persisted_user):
        await repo.update_password(str(persisted_user.id), "$argon2id$prov", True)
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.must_change_password is True

    async def test_unknown_id_is_a_noop(self, repo, persisted_user):
        await repo.update_password(str(uuid.uuid4()), "$argon2id$x", False)
        unchanged = await repo.get_by_id(str(persisted_user.id))
        assert unchanged.password_hash == "$argon2id$placeholder"


class TestUpdateStatus:
    @pytest.mark.parametrize(
        "status",
        [UserStatus.ACTIVE, UserStatus.BLOCKED, UserStatus.SUSPENDED],
    )
    async def test_sets_each_valid_status(self, repo, persisted_user, status):
        await repo.update_status(str(persisted_user.id), status.value)
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.status == status

    async def test_blocking_flips_is_active(self, repo, persisted_user):
        await repo.update_status(str(persisted_user.id), UserStatus.BLOCKED.value)
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.is_active is False
        assert updated.is_blocked is True


class TestUpdateLastLogin:
    async def test_records_timestamp(self, repo, persisted_user):
        moment = datetime.now(timezone.utc)
        await repo.update_last_login(str(persisted_user.id), moment)
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.last_login_at is not None


class TestSetProvisionalPasswordSent:
    async def test_records_timestamp(self, repo, persisted_user):
        assert (await repo.get_by_id(str(persisted_user.id))).provisional_password_sent_at is None

        await repo.set_provisional_password_sent(
            str(persisted_user.id), datetime.now(timezone.utc)
        )
        updated = await repo.get_by_id(str(persisted_user.id))
        assert updated.provisional_password_sent_at is not None


class TestListAndCount:
    async def test_lists_created_users(self, repo, seeded_profiles):
        for i in range(3):
            await _make_user(repo, seeded_profiles, f"user{i}", f"user{i}@example.com")
        assert len(await repo.list_all()) == 3

    async def test_respects_limit(self, repo, seeded_profiles):
        for i in range(5):
            await _make_user(repo, seeded_profiles, f"user{i}", f"user{i}@example.com")
        assert len(await repo.list_all(limit=2)) == 2

    async def test_respects_offset(self, repo, seeded_profiles):
        for i in range(5):
            await _make_user(repo, seeded_profiles, f"user{i}", f"user{i}@example.com")
        assert len(await repo.list_all(limit=10, offset=3)) == 2

    async def test_empty_table_returns_empty_list(self, repo, seeded_profiles):
        assert await repo.list_all() == []

    async def test_count_all_ignores_pagination(self, repo, seeded_profiles):
        """count_all é o total da tabela — é o que alimenta PaginatedResponse.total."""
        for i in range(7):
            await _make_user(repo, seeded_profiles, f"user{i}", f"user{i}@example.com")

        page = await repo.list_all(limit=2, offset=0)
        assert len(page) == 2
        assert await repo.count_all() == 7

    async def test_count_all_on_empty_table(self, repo, seeded_profiles):
        assert await repo.count_all() == 0


class TestGetProfileByName:
    async def test_finds_file_editor(self, repo, seeded_profiles):
        profile = await repo.get_profile_by_name(ProfileName.FILE_EDITOR.value)
        assert profile is not None
        assert profile.name == ProfileName.FILE_EDITOR.value

    async def test_finds_airline_company(self, repo, seeded_profiles):
        profile = await repo.get_profile_by_name(ProfileName.AIRLINE_COMPANY.value)
        assert profile is not None

    async def test_returns_none_for_unknown_profile(self, repo, seeded_profiles):
        assert await repo.get_profile_by_name("inexistente") is None


class TestRefreshTokens:
    async def _create(self, repo, user, token_hash="hash-1", **kwargs):
        params = {
            "user_id": str(user.id),
            "token_hash": token_hash,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        }
        params.update(kwargs)
        await repo.create_refresh_token(**params)

    async def test_create_and_fetch(self, repo, persisted_user):
        await self._create(repo, persisted_user)
        stored = await repo.get_refresh_token("hash-1")
        assert stored is not None
        assert stored.revoked is False

    async def test_stores_ip_and_user_agent(self, repo, persisted_user):
        await self._create(
            repo, persisted_user, ip_address="203.0.113.42", user_agent="pytest/1.0"
        )
        stored = await repo.get_refresh_token("hash-1")
        assert stored.ip_address == "203.0.113.42"
        assert stored.user_agent == "pytest/1.0"

    async def test_unknown_hash_returns_none(self, repo, persisted_user):
        assert await repo.get_refresh_token("nao-existe") is None

    async def test_revoke_single_token(self, repo, persisted_user):
        await self._create(repo, persisted_user, "hash-a")
        await self._create(repo, persisted_user, "hash-b")

        await repo.revoke_refresh_token("hash-a")

        assert (await repo.get_refresh_token("hash-a")).revoked is True
        assert (await repo.get_refresh_token("hash-b")).revoked is False

    async def test_revoke_all_for_user(self, repo, persisted_user):
        for h in ("hash-a", "hash-b", "hash-c"):
            await self._create(repo, persisted_user, h)

        await repo.revoke_all_refresh_tokens(str(persisted_user.id))

        for h in ("hash-a", "hash-b", "hash-c"):
            assert (await repo.get_refresh_token(h)).revoked is True

    async def test_revoke_all_does_not_touch_other_users(
        self, repo, persisted_user, seeded_profiles
    ):
        other = await _make_user(repo, seeded_profiles, "outro", "outro@example.com")
        await self._create(repo, persisted_user, "hash-do-persisted")
        await repo.create_refresh_token(
            user_id=other.id,
            token_hash="hash-do-outro",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )

        await repo.revoke_all_refresh_tokens(str(persisted_user.id))

        assert (await repo.get_refresh_token("hash-do-persisted")).revoked is True
        assert (await repo.get_refresh_token("hash-do-outro")).revoked is False

    async def test_revoking_unknown_hash_is_a_noop(self, repo, persisted_user):
        await self._create(repo, persisted_user, "hash-a")
        await repo.revoke_refresh_token("nao-existe")
        assert (await repo.get_refresh_token("hash-a")).revoked is False
