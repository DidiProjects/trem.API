#!/usr/bin/env python
"""
Gestão de clientes de API pela linha de comando.

Existe para o bootstrap: o primeiro cliente precisa ser criado antes de haver
qualquer chave válida para chamar /admin/clients. Roda contra o banco
diretamente, então precisa de DATABASE_URL apontando para o Postgres.

Uso:
    python -m scripts.manage_api_clients create flight-api --profile airline_company
    python -m scripts.manage_api_clients list
    python -m scripts.manage_api_clients revoke <client_id>

No servidor, dentro do container:
    docker exec -it trem-api python -m scripts.manage_api_clients list
"""
import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.domain.entities.profile import ProfileName
from app.services.api_client_service import ApiClientService


def _session_factory():
    engine = create_async_engine(get_settings().DATABASE_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def cmd_create(args) -> int:
    engine, Session = _session_factory()
    try:
        async with Session() as session:
            try:
                client, raw_key = await ApiClientService(session).create_client(
                    name=args.name,
                    profile_name=args.profile,
                    description=args.description,
                )
            except AppException as e:
                print(f"erro: {e.message}", file=sys.stderr)
                return 1
            await session.commit()

        print(f"cliente criado: {client.name}  (perfil: {client.profile_name})")
        print(f"id:      {client.id}")
        print(f"api key: {raw_key}")
        print()
        print("Guarde a chave agora — ela não pode ser recuperada depois.")
        return 0
    finally:
        await engine.dispose()


async def cmd_list(args) -> int:
    engine, Session = _session_factory()
    try:
        async with Session() as session:
            clients, total = await ApiClientService(session).list_clients(limit=200)

        if not clients:
            print("nenhum cliente cadastrado")
            return 0

        print(f"{'NOME':<24} {'PERFIL':<18} {'STATUS':<10} {'ÚLTIMO USO':<20} ID")
        for c in clients:
            status = "revogado" if c.revoked else "ativo"
            last = c.last_used_at.strftime("%Y-%m-%d %H:%M") if c.last_used_at else "nunca"
            print(f"{c.name:<24} {c.profile_name:<18} {status:<10} {last:<20} {c.id}")
        print(f"\ntotal: {total}")
        return 0
    finally:
        await engine.dispose()


async def cmd_revoke(args) -> int:
    engine, Session = _session_factory()
    try:
        async with Session() as session:
            try:
                await ApiClientService(session).revoke_client(args.client_id)
            except AppException as e:
                print(f"erro: {e.message}", file=sys.stderr)
                return 1
            await session.commit()
        print(f"cliente {args.client_id} revogado")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestão de clientes de API")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="cria um cliente e emite a chave")
    create.add_argument("name", help="identificador, ex: flight-api")
    create.add_argument(
        "--profile",
        required=True,
        choices=[p.value for p in ProfileName],
        help="perfil de acesso do cliente",
    )
    create.add_argument("--description", default=None)
    create.set_defaults(func=cmd_create)

    listing = sub.add_parser("list", help="lista os clientes cadastrados")
    listing.set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", help="revoga a chave de um cliente")
    revoke.add_argument("client_id")
    revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
