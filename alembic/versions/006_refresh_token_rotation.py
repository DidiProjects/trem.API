"""refresh tokens: rotação com detecção de reuso, ligados a uma aplicação

Cada refresh vira um novo token e o anterior fica revogado, apontando para o
sucessor (replaced_by). Todos os tokens de um mesmo login dividem a family_id: se
um token já trocado volta a ser usado, alguém tem uma cópia dele, e a família
inteira é revogada.

application_id é a aplicação para a qual o login foi feito: o refresh só emite
access tokens para ela. Tokens existentes viram famílias de um só e ficam na `trem`.

Revision ID: 006
Revises: 005
Create Date: 2026-11-16 00:00:01.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("family_id", sa.Uuid(as_uuid=True), nullable=True))
    op.execute("UPDATE refresh_tokens SET family_id = id")
    op.alter_column("refresh_tokens", "family_id", nullable=False)
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.add_column(
        "refresh_tokens",
        sa.Column(
            "replaced_by", sa.Uuid(as_uuid=True),
            sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL", name="refresh_tokens_replaced_by_fkey"),
            nullable=True,
        ),
    )

    op.add_column("refresh_tokens", sa.Column("application_id", sa.Uuid(as_uuid=True), nullable=True))
    op.execute("UPDATE refresh_tokens SET application_id = (SELECT id FROM applications WHERE slug = 'trem')")
    op.alter_column("refresh_tokens", "application_id", nullable=False)
    op.create_foreign_key(
        "refresh_tokens_application_id_fkey", "refresh_tokens", "applications",
        ["application_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("refresh_tokens_application_id_fkey", "refresh_tokens", type_="foreignkey")
    op.drop_column("refresh_tokens", "application_id")
    op.drop_column("refresh_tokens", "replaced_by")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
