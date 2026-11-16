"""aplicações e permissões dinâmicas

Os perfis eram um enum fixo, travado por CHECK: cada acesso novo exigia uma
migration. Passam a ser permissões de uma aplicação, cadastráveis pela API, para
que outras interfaces autentiquem pela trem.API com permissões próprias.

  - applications: cada interface que confia na trem.API; o slug é o `aud` do token
  - profiles vira permissions, ligada à aplicação `trem`; o nome é único por aplicação
  - user_profiles / api_client_profiles viram user_permissions / api_client_permissions,
    com granted_at (e granted_by nos usuários) para auditoria
  - nova permissão trem:admin, concedida a ninguém aqui: o primeiro admin sai do
    script de bootstrap

Nenhum acesso muda: cada usuário e chave mantém exatamente as linhas que tinha.

users.profile_id e api_clients.profile_id seguem como na 004, agora apontando
para permissions.

Revision ID: 005
Revises: 004
Create Date: 2026-11-16 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROFILES = ("file_editor", "airline_company", "ai", "linkedin_scraper")


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO applications (slug, name, description) "
        "VALUES ('trem', 'trem.API', 'Arquivos, IA e scrapers desta API')"
    )

    op.drop_constraint("ck_profiles_name", "profiles", type_="check")
    op.drop_constraint("profiles_name_key", "profiles", type_="unique")
    op.rename_table("profiles", "permissions")
    op.execute("ALTER INDEX profiles_pkey RENAME TO permissions_pkey")
    op.add_column(
        "permissions",
        sa.Column("application_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE permissions SET application_id = (SELECT id FROM applications WHERE slug = 'trem')")
    op.alter_column("permissions", "application_id", nullable=False)
    op.create_foreign_key(
        "permissions_application_id_fkey", "permissions", "applications",
        ["application_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_permissions_application_id_name", "permissions", ["application_id", "name"]
    )
    op.execute("""
        INSERT INTO permissions (name, description, application_id)
        SELECT 'admin', 'Administração de usuários, aplicações e chaves', id
        FROM applications WHERE slug = 'trem'
    """)

    for old, new, owner in (
        ("user_profiles", "user_permissions", "user_id"),
        ("api_client_profiles", "api_client_permissions", "client_id"),
    ):
        op.rename_table(old, new)
        op.alter_column(new, "profile_id", new_column_name="permission_id")
        op.execute(f"ALTER INDEX {old}_pkey RENAME TO {new}_pkey")
        op.execute(f"ALTER TABLE {new} RENAME CONSTRAINT {old}_{owner}_fkey TO {new}_{owner}_fkey")
        op.execute(
            f"ALTER TABLE {new} RENAME CONSTRAINT {old}_profile_id_fkey TO {new}_permission_id_fkey"
        )
        op.add_column(
            new,
            sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    op.add_column(
        "user_permissions",
        sa.Column(
            "granted_by", sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="user_permissions_granted_by_fkey"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """
    Só os perfis da aplicação `trem` cabem no esquema antigo. Permissões de outras
    aplicações e trem:admin são apagadas, com as concessões que tinham.
    """
    names = ", ".join(f"'{n}'" for n in _PROFILES)
    op.execute(f"""
        DELETE FROM permissions
        WHERE application_id <> (SELECT id FROM applications WHERE slug = 'trem')
           OR name NOT IN ({names})
    """)

    op.drop_column("user_permissions", "granted_by")
    for old, new, owner in (
        ("user_profiles", "user_permissions", "user_id"),
        ("api_client_profiles", "api_client_permissions", "client_id"),
    ):
        op.drop_column(new, "granted_at")
        op.execute(
            f"ALTER TABLE {new} RENAME CONSTRAINT {new}_permission_id_fkey TO {old}_profile_id_fkey"
        )
        op.execute(f"ALTER TABLE {new} RENAME CONSTRAINT {new}_{owner}_fkey TO {old}_{owner}_fkey")
        op.execute(f"ALTER INDEX {new}_pkey RENAME TO {old}_pkey")
        op.alter_column(new, "permission_id", new_column_name="profile_id")
        op.rename_table(new, old)

    op.drop_constraint("uq_permissions_application_id_name", "permissions", type_="unique")
    op.drop_constraint("permissions_application_id_fkey", "permissions", type_="foreignkey")
    op.drop_column("permissions", "application_id")
    op.execute("ALTER INDEX permissions_pkey RENAME TO profiles_pkey")
    op.rename_table("permissions", "profiles")
    op.create_unique_constraint("profiles_name_key", "profiles", ["name"])
    op.create_check_constraint("ck_profiles_name", "profiles", f"name IN ({names})")
    op.drop_table("applications")
