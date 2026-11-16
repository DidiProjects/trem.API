"""segundo fator TOTP e códigos de recuperação

  - users.totp_secret: segredo cifrado (AES-256-GCM, chave fora do banco)
  - users.totp_enabled_at: só depois de confirmado com um código válido
  - users.totp_last_step: último passo de tempo aceito, contra reuso do mesmo código
  - mfa_recovery_codes: códigos de uso único, guardados como SHA-256
  - refresh_tokens.amr: como o login foi provado (pwd, pwd+otp), herdado pelos refreshes

Revision ID: 008
Revises: 007
Create Date: 2026-11-16 00:00:03.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.Text, nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger, nullable=True))

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"])

    op.add_column(
        "refresh_tokens",
        sa.Column("amr", sa.String(20), nullable=False, server_default="pwd"),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "amr")
    op.drop_index("ix_mfa_recovery_codes_user_id", table_name="mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_enabled_at")
    op.drop_column("users", "totp_secret")
