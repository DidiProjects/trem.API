"""api_clients — autenticacao por cliente

Cada aplicacao consumidora (outra API, script, playground) passa a ter a sua
propria chave, com perfil e revogacao individuais, substituindo a chave unica
compartilhada em settings.API_KEY.

key_hash e SHA-256 (64 hex chars) da chave completa, nao Argon2id: a chave tem
256 bits de entropia, entao nao ha dicionario a atacar, e o hash sem salt
permite lookup por indice em vez de varredura da tabela a cada request.

Revision ID: 003
Revises: 002
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "profile_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("profiles.id"),
            nullable=False,
        ),
        sa.Column(
            "revoked", sa.Boolean, nullable=False, server_default="false"
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    # key_hash e o caminho quente: toda request autenticada faz esse lookup
    op.create_index("ix_api_clients_key_hash", "api_clients", ["key_hash"])
    op.create_index("ix_api_clients_name", "api_clients", ["name"])
    op.create_index("ix_api_clients_revoked", "api_clients", ["revoked"])


def downgrade() -> None:
    op.drop_index("ix_api_clients_revoked", table_name="api_clients")
    op.drop_index("ix_api_clients_name", table_name="api_clients")
    op.drop_index("ix_api_clients_key_hash", table_name="api_clients")
    op.drop_table("api_clients")
