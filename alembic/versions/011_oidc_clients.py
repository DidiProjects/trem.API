"""login único (OpenID Connect) para os serviços do hub

  - oidc_clients: um client OIDC por serviço do hub (client_id = slug), com o hash do
    segredo e as redirect URIs aceitas; some junto com o serviço

Revision ID: 011
Revises: 010
Create Date: 2026-11-20 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oidc_clients",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("hub_service_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("hub_services.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("secret_created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("redirect_uris", sa.JSON, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("oidc_clients")
