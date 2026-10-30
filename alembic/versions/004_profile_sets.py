"""perfis como conjunto, perfis ai e linkedin_scraper, chave ligada a usuário

Cada usuário e cada chave tinham um único perfil (profile_id). Passam a ter um
conjunto (user_profiles, api_client_profiles), para que a mesma conta acesse
arquivos, IA e scraper.

Migração dos dados:
  - o perfil atual de cada usuário e de cada chave vai para a tabela de junção
  - todo usuário existente recebe todos os perfis
  - toda chave existente recebe também `ai`: até aqui /llm aceitava qualquer
    chave, então é o que preserva o acesso de hoje sem conceder linkedin_scraper

api_clients.user_id é opcional: chave de serviço não tem pessoa por trás.

profile_id fica nas duas tabelas, só que aceitando nulo e sem uso pelo código:
é o que deixa o downgrade devolver a cada linha antiga o perfil exato que ela
tinha. Sai numa migration posterior, depois de validada esta em produção.

Revision ID: 004
Revises: 003
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY = ("file_editor", "airline_company")
_ALL = _LEGACY + ("ai", "linkedin_scraper")


def _names_sql(names: tuple[str, ...]) -> str:
    return "name IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_profiles_name", "profiles", type_="check")
    op.create_check_constraint("ck_profiles_name", "profiles", _names_sql(_ALL))
    op.execute("""
        INSERT INTO profiles (name, description) VALUES
        ('ai',               'Acesso ao modelo de linguagem (/llm)'),
        ('linkedin_scraper', 'Acesso aos jobs de scraping do LinkedIn')
    """)

    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", sa.Uuid(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "api_client_profiles",
        sa.Column("client_id", sa.Uuid(as_uuid=True), sa.ForeignKey("api_clients.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("profile_id", sa.Uuid(as_uuid=True), sa.ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    )

    op.execute("INSERT INTO user_profiles (user_id, profile_id) SELECT id, profile_id FROM users")
    op.execute(
        "INSERT INTO api_client_profiles (client_id, profile_id) "
        "SELECT id, profile_id FROM api_clients"
    )
    op.execute("""
        INSERT INTO user_profiles (user_id, profile_id)
        SELECT u.id, p.id FROM users u CROSS JOIN profiles p
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO api_client_profiles (client_id, profile_id)
        SELECT c.id, p.id FROM api_clients c CROSS JOIN profiles p WHERE p.name = 'ai'
        ON CONFLICT DO NOTHING
    """)

    op.add_column(
        "api_clients",
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_api_clients_user_id", "api_clients", ["user_id"])

    op.alter_column("users", "profile_id", nullable=True)
    op.alter_column("api_clients", "profile_id", nullable=True)


def downgrade() -> None:
    """
    Linhas anteriores à 004 recuperam o profile_id que nunca saiu do lugar.
    Linhas criadas depois ficam com um perfil legado que tenham; quem só tem
    perfis novos não cabe no esquema antigo: o usuário é bloqueado e a chave
    revogada, em vez de ganhar um acesso que não tinha.
    """
    for table, link, key in (
        ("users", "user_profiles", "user_id"),
        ("api_clients", "api_client_profiles", "client_id"),
    ):
        op.execute(f"""
            UPDATE {table} t SET profile_id = (
                SELECT l.profile_id FROM {link} l JOIN profiles p ON p.id = l.profile_id
                WHERE l.{key} = t.id AND p.{_names_sql(_LEGACY)}
                ORDER BY p.name LIMIT 1
            )
            WHERE t.profile_id IS NULL
        """)

    op.execute(
        "UPDATE users SET status = 'blocked', "
        "profile_id = (SELECT id FROM profiles WHERE name = 'file_editor') "
        "WHERE profile_id IS NULL"
    )
    op.execute(
        "UPDATE api_clients SET revoked = true, "
        "profile_id = (SELECT id FROM profiles WHERE name = 'file_editor') "
        "WHERE profile_id IS NULL"
    )
    op.alter_column("users", "profile_id", nullable=False)
    op.alter_column("api_clients", "profile_id", nullable=False)

    op.drop_index("ix_api_clients_user_id", table_name="api_clients")
    op.drop_column("api_clients", "user_id")
    op.drop_table("api_client_profiles")
    op.drop_table("user_profiles")

    op.execute("DELETE FROM profiles WHERE name IN ('ai', 'linkedin_scraper')")
    op.drop_constraint("ck_profiles_name", "profiles", type_="check")
    op.create_check_constraint("ck_profiles_name", "profiles", _names_sql(_LEGACY))
