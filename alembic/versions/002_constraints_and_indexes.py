"""constraints de integridade e índices de performance

Adiciona:
  - CHECK em users.status e profiles.name (impede estados inválidos no banco,
    não só na camada de aplicação)
  - índice composto (user_id, revoked) em refresh_tokens — usado por
    revoke_all_refresh_tokens e pela busca de tokens vivos de um usuário
  - índice em refresh_tokens.expires_at — usado na limpeza de tokens expirados

Revision ID: 002
Revises: 001
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Normaliza qualquer linha fora dos estados válidos antes de aplicar o CHECK
    op.execute(
        "UPDATE users SET status = 'blocked' "
        "WHERE status NOT IN ('blocked', 'active', 'suspended')"
    )

    op.create_check_constraint(
        "ck_users_status",
        "users",
        "status IN ('blocked', 'active', 'suspended')",
    )
    op.create_check_constraint(
        "ck_profiles_name",
        "profiles",
        "name IN ('file_editor', 'airline_company')",
    )

    op.create_index(
        "ix_refresh_tokens_user_id_revoked",
        "refresh_tokens",
        ["user_id", "revoked"],
    )
    op.create_index(
        "ix_refresh_tokens_expires_at",
        "refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id_revoked", table_name="refresh_tokens")
    op.drop_constraint("ck_profiles_name", "profiles", type_="check")
    op.drop_constraint("ck_users_status", "users", type_="check")
