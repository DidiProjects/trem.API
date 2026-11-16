#!/usr/bin/env python
"""
Cria o primeiro admin, ou promove uma conta existente.

Existe porque a API de administração exige um admin logado com segundo fator: o
primeiro precisa nascer fora dela. Roda contra o banco diretamente.

Uso, no servidor:
    docker exec -it trem-api python -m scripts.bootstrap_admin diego --email voce@example.com
    docker exec -it trem-api python -m scripts.bootstrap_admin diego --permission trem:ai

Conta nova recebe uma senha provisória, mostrada só aqui, e precisa trocá-la no
primeiro login. Depois: cadastrar o TOTP (/auth/mfa/enroll e /auth/mfa/confirm) e
entrar de novo com o código; só então as rotas /admin abrem.
"""
import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import generate_provisional_password, hash_password
from app.domain.entities.permission import TremPermission, split_permission_key
from app.domain.entities.user import UserStatus
from app.repositories.permission_repository import PermissionRepository
from app.repositories.user_repository import UserRepository


async def bootstrap(username: str, email: str | None, extra: list[str]) -> int:
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            users = UserRepository(session)
            keys = sorted({TremPermission.ADMIN.value, *extra})
            found = await PermissionRepository(session).get_by_keys(keys)
            missing = set(keys) - {p.key for p in found}
            if missing:
                print(f"erro: permissões não encontradas: {', '.join(sorted(missing))}", file=sys.stderr)
                return 1

            user = await users.get_by_username(username)
            password = None
            if user is None:
                if not email:
                    print("erro: conta nova precisa de --email", file=sys.stderr)
                    return 1
                password = generate_provisional_password(20)
                user = await users.create(
                    username=username,
                    password_hash=hash_password(password),
                    permission_ids=[],
                    email=email.lower(),
                    status=UserStatus.ACTIVE.value,
                    must_change_password=True,
                )
            elif user.status != UserStatus.ACTIVE:
                await users.update_status(user.id, UserStatus.ACTIVE.value)

            current = await PermissionRepository(session).get_by_keys(user.permissions)
            ids = {p.id for p in current} | {p.id for p in found}
            await users.replace_permissions(user.id, list(ids), granted_by=None)
            await session.commit()
            user = await users.get_by_id(user.id)

        print(f"admin: {user.username}  ({', '.join(user.permissions)})")
        if password:
            print(f"senha provisória: {password}")
            print("Troque no primeiro login. Ela não pode ser vista de novo.")
        if not user.totp_enabled:
            print("Próximo passo: cadastrar o TOTP; as rotas /admin exigem login com segundo fator.")
        return 0
    finally:
        await engine.dispose()


def _permission(value: str) -> str:
    try:
        split_permission_key(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Cria ou promove um admin")
    parser.add_argument("username")
    parser.add_argument("--email", default=None, help="obrigatório para conta nova")
    parser.add_argument("--permission", action="append", default=[], type=_permission,
                        help="permissão extra, ex: trem:ai; repita para várias")
    args = parser.parse_args()
    return asyncio.run(bootstrap(args.username, args.email, args.permission))


if __name__ == "__main__":
    raise SystemExit(main())
