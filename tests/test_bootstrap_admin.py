"""scripts.bootstrap_admin contra um banco em arquivo, como roda no servidor."""
import asyncio
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import verify_password
from app.domain.entities.permission import TremPermission
from app.infrastructure.database.models import ApplicationModel, Base, PermissionModel, UserModel


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}"

    async def setup():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as session:
            trem = ApplicationModel(slug="trem", name="trem.API")
            permissions = {k.value: PermissionModel(application=trem, name=k.value.split(":", 1)[1]) for k in TremPermission}
            session.add_all(permissions.values())
            session.add(UserModel(username="existente", email="e@example.com", password_hash="x",
                                  status="blocked", permissions=[permissions["trem:ai"]]))
            await session.commit()
        await engine.dispose()

    asyncio.run(setup())
    monkeypatch.setattr(get_settings(), "DATABASE_URL", url)
    return url


def _user(url, username):
    async def go():
        engine = create_async_engine(url)
        async with async_sessionmaker(engine)() as session:
            user = (await session.execute(select(UserModel).where(UserModel.username == username))).scalar_one()
            keys = sorted(p.key for p in user.permissions)
        await engine.dispose()
        return user, keys
    return asyncio.run(go())


def _run(monkeypatch, *argv):
    import scripts.bootstrap_admin as cli

    monkeypatch.setattr(sys, "argv", ["bootstrap_admin", *argv])
    return cli.main()


def test_creates_an_admin_with_a_provisional_password(db_url, monkeypatch, capsys):
    assert _run(monkeypatch, "diego", "--email", "Diego@Example.com", "--permission", "trem:ai") == 0

    out = capsys.readouterr().out
    password = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("senha provisória"))
    user, keys = _user(db_url, "diego")
    assert keys == ["trem:admin", "trem:ai"]
    assert user.status == "active" and user.must_change_password is True
    assert user.email == "diego@example.com"
    assert verify_password(password, user.password_hash)
    assert "segundo fator" in out


def test_promotes_an_existing_account_keeping_its_permissions(db_url, monkeypatch, capsys):
    assert _run(monkeypatch, "existente") == 0

    user, keys = _user(db_url, "existente")
    assert keys == ["trem:admin", "trem:ai"]
    assert user.status == "active"
    assert user.password_hash == "x"  # password untouched
    assert "senha provisória" not in capsys.readouterr().out


def test_new_account_needs_an_email(db_url, monkeypatch):
    assert _run(monkeypatch, "novo") == 1


def test_unknown_permission_fails(db_url, monkeypatch):
    assert _run(monkeypatch, "existente", "--permission", "trem:root") == 1
