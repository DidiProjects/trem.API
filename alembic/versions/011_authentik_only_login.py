"""login só pelo authentik: sai o login próprio

  - apaga refresh_tokens, email_tokens e mfa_recovery_codes
  - users perde senha, status, troca obrigatória, senha provisória, último login, e-mail
    confirmado, aprovação e TOTP: o authentik guarda tudo isso e decide quem entra
  - as contas continuam (ligadas ao authentik pelo username), com suas permissões e chaves

O downgrade recria a estrutura, sem os dados: as contas voltam ativas e sem senha.

Revision ID: 011
Revises: 010
Create Date: 2026-11-22 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUSES = ("blocked", "active", "suspended", "pending_verification", "pending_approval", "rejected")
# Not an argon2 hash: no password matches it (trem.API's marker for authentik accounts).
_NO_PASSWORD = "!authentik"


def upgrade() -> None:
    op.drop_table("mfa_recovery_codes")
    op.drop_table("email_tokens")
    op.drop_table("refresh_tokens")

    op.drop_constraint("ck_users_status", "users", type_="check")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_constraint("users_approved_by_fkey", "users", type_="foreignkey")
    for column in (
        "password_hash", "status", "must_change_password", "provisional_password_sent_at",
        "last_login_at", "email_verified_at", "approved_at", "approved_by",
        "totp_secret", "totp_enabled_at", "totp_last_step",
    ):
        op.drop_column("users", column)


def downgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.Text, nullable=False, server_default=_NO_PASSWORD))
    op.alter_column("users", "password_hash", server_default=None)
    op.add_column("users", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.alter_column("users", "status", server_default="blocked")
    op.create_index("ix_users_status", "users", ["status"])
    op.create_check_constraint("ck_users_status", "users", "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")")
    op.add_column("users", sa.Column("must_change_password", sa.Boolean, nullable=False, server_default="false"))
    op.alter_column("users", "must_change_password", server_default="true")
    op.add_column("users", sa.Column("provisional_password_sent_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "approved_by", sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="users_approved_by_fkey"),
            nullable=True,
        ),
    )
    op.add_column("users", sa.Column("totp_secret", sa.Text, nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger, nullable=True))

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("ip_address", postgresql.INET, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("family_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "replaced_by", sa.Uuid(as_uuid=True),
            sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL", name="refresh_tokens_replaced_by_fkey"),
            nullable=True,
        ),
        sa.Column(
            "application_id", sa.Uuid(as_uuid=True),
            sa.ForeignKey("applications.id", ondelete="CASCADE", name="refresh_tokens_application_id_fkey"),
            nullable=False,
        ),
        sa.Column("amr", sa.String(20), nullable=False, server_default="pwd"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"])

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mfa_recovery_codes_user_id", "mfa_recovery_codes", ["user_id"])
