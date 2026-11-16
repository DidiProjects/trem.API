"""chaves de API do próprio usuário

  - o nome da chave deixa de ser único no sistema inteiro: é único entre as chaves
    ativas do mesmo dono (dois usuários podem ter uma chave "notebook"), e entre as
    chaves de serviço ativas, como antes
  - api_clients.key_hint: o começo da chave (trem_xxxx), para reconhecê-la na lista
    sem guardar a chave; chaves antigas ficam sem

Revision ID: 009
Revises: 008
Create Date: 2026-11-16 00:00:04.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("api_clients_name_key", "api_clients", type_="unique")
    op.create_index(
        "uq_api_clients_owner_name", "api_clients", ["user_id", "name"], unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND NOT revoked"),
    )
    op.create_index(
        "uq_api_clients_service_name", "api_clients", ["name"], unique=True,
        postgresql_where=sa.text("user_id IS NULL AND NOT revoked"),
    )
    op.add_column("api_clients", sa.Column("key_hint", sa.String(12), nullable=True))


def downgrade() -> None:
    """Nomes repetidos entre donos não cabem no esquema antigo: ganham o sufixo do id."""
    op.drop_column("api_clients", "key_hint")
    op.drop_index("uq_api_clients_service_name", table_name="api_clients")
    op.drop_index("uq_api_clients_owner_name", table_name="api_clients")
    op.execute("""
        UPDATE api_clients c SET name = left(c.name, 91) || '-' || left(c.id::text, 8)
        WHERE EXISTS (SELECT 1 FROM api_clients o WHERE o.name = c.name AND o.id <> c.id)
    """)
    op.create_unique_constraint("api_clients_name_key", "api_clients", ["name"])
