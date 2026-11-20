"""pedidos e códigos de login único (OpenID Connect)

  - oidc_grants: o pedido que um serviço abriu em /oauth/authorize e, depois de aprovado
    no trem.FRONT, o código de uso único que ele troca por tokens; pedido e código só
    como SHA-256; somem com o client e com o usuário

Revision ID: 012
Revises: 011
Create Date: 2026-11-20 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oidc_grants",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("oidc_client_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("oidc_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("scope", sa.String(200), nullable=False),
        sa.Column("state", sa.String(500), nullable=False),
        sa.Column("nonce", sa.String(500), nullable=True),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("amr", sa.JSON, nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("code_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_oidc_grants_expires_at", "oidc_grants", ["expires_at"])


def downgrade() -> None:
    op.drop_table("oidc_grants")
