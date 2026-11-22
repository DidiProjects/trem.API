"""
Testes de UserRepository contra um banco real (SQLite in-memory).

Diferente do resto da suíte, aqui nada é mockado: exercita o SQL de verdade,
os defaults dos modelos e o mapeamento ORM -> entidade de domínio.
"""
import uuid

import pytest

from app.domain.entities.permission import TremPermission
from app.domain.entities.user import User
from app.repositories.user_repository import UserRepository, _as_uuid


@pytest.fixture
def repo(db_session):
    return UserRepository(db_session)


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
    async def test_returns_domain_entity_without_permissions(self, repo, seeded_permissions):
        user = await repo.create(username="novo", email="novo@example.com")
        assert isinstance(user, User)
        assert (user.username, user.email, user.permissions) == ("novo", "novo@example.com", [])

    async def test_generates_id_and_timestamps(self, repo, seeded_permissions):
        user = await repo.create(username="novo")
        assert uuid.UUID(user.id)
        assert user.created_at is not None and user.updated_at is not None

    async def test_email_is_optional(self, repo, seeded_permissions):
        assert (await repo.create(username="sememail")).email is None


class TestGetById:
    async def test_finds_existing_user(self, repo, persisted_user):
        found = await repo.get_by_id(str(persisted_user.id))
        assert found is not None
        assert found.username == "persisted"

    async def test_returns_none_for_unknown_id(self, repo, seeded_permissions):
        assert await repo.get_by_id(str(uuid.uuid4())) is None

    async def test_malformed_id_returns_none_instead_of_raising(self, repo, seeded_permissions):
        """Id inválido na URL deve virar 404, não erro de driver."""
        assert await repo.get_by_id("nao-e-uuid") is None

    async def test_loads_profile_relationship(self, repo, persisted_user):
        found = await repo.get_by_id(str(persisted_user.id))
        assert found.permissions == [TremPermission.FILE_EDITOR.value]


class TestGetByUsername:
    async def test_finds_existing_user(self, repo, persisted_user):
        found = await repo.get_by_username("persisted")
        assert found is not None
        assert found.id == str(persisted_user.id)

    async def test_returns_none_for_unknown_username(self, repo, seeded_permissions):
        assert await repo.get_by_username("ninguem") is None

    async def test_is_case_sensitive(self, repo, persisted_user):
        assert await repo.get_by_username("PERSISTED") is None


class TestGetByEmail:
    async def test_ignores_case(self, repo, persisted_user):
        found = await repo.get_by_email("PERSISTED@example.com")
        assert found is not None and found.username == "persisted"

    async def test_returns_none_for_unknown_email(self, repo, persisted_user):
        assert await repo.get_by_email("ninguem@example.com") is None


class TestReplacePermissions:
    async def test_replaces_the_set_and_the_lookups_see_it(self, repo, persisted_user, seeded_permissions):
        wanted = [seeded_permissions[TremPermission.AI.value], seeded_permissions[TremPermission.LINKEDIN_SCRAPER.value]]
        await repo.replace_permissions(str(persisted_user.id), wanted, granted_by=None)

        found = await repo.get_by_id(str(persisted_user.id))
        assert found.permissions == [TremPermission.AI.value, TremPermission.LINKEDIN_SCRAPER.value]

    async def test_empty_set_removes_every_permission(self, repo, persisted_user):
        await repo.replace_permissions(str(persisted_user.id), [], granted_by=None)
        assert (await repo.get_by_id(str(persisted_user.id))).permissions == []


class TestPermissionRepository:
    @pytest.fixture
    def permissions(self, db_session):
        from app.repositories.permission_repository import PermissionRepository

        return PermissionRepository(db_session)

    async def test_finds_by_keys(self, permissions, seeded_permissions):
        found = await permissions.get_by_keys(["trem:file_editor", "trem:ai"])
        assert {p.key for p in found} == {"trem:file_editor", "trem:ai"}

    async def test_unknown_keys_are_left_out(self, permissions, seeded_permissions):
        found = await permissions.get_by_keys(["trem:ai", "trem:inexistente", "outra:ai"])
        assert [p.key for p in found] == ["trem:ai"]

    async def test_name_only_matches_within_its_application(
        self, db_session, permissions, seeded_permissions
    ):
        from app.infrastructure.database.models import ApplicationModel, PermissionModel

        db_session.add(PermissionModel(application=ApplicationModel(slug="financas", name="F"), name="read"))
        await db_session.flush()
        assert await permissions.get_by_keys(["trem:read"]) == []
        assert [p.key for p in await permissions.get_by_keys(["financas:read"])] == ["financas:read"]

    async def test_empty_input(self, permissions, seeded_permissions):
        assert await permissions.get_by_keys([]) == []

    async def test_finds_by_ids(self, permissions, seeded_permissions):
        found = await permissions.get_by_ids([seeded_permissions["trem:ai"], "nao-e-uuid"])
        assert [p.key for p in found] == ["trem:ai"]
