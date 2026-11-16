"""cadastro pelo próprio usuário: confirmação de e-mail e aprovação

  - novos status: pending_verification (e-mail ainda não confirmado),
    pending_approval (aguardando o admin) e rejected
  - users.email_verified_at, approved_at, approved_by
  - email_tokens: tokens de uso único enviados por e-mail, guardados como SHA-256

Contas existentes não mudam de status.

Revision ID: 007
Revises: 006
Create Date: 2026-11-16 00:00:02.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = ("blocked", "active", "suspended")
_NEW = _OLD + ("pending_verification", "pending_approval", "rejected")


def _status_check(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_users_status", "users", type_="check")
    op.create_check_constraint("ck_users_status", "users", _status_check(_NEW))

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


def downgrade() -> None:
    """Contas do cadastro próprio ainda não aprovadas não cabem no esquema antigo: viram blocked."""
    op.drop_index("ix_email_tokens_user_id", table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_column("users", "approved_by")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "email_verified_at")

    op.drop_constraint("ck_users_status", "users", type_="check")
    op.execute("UPDATE users SET status = 'blocked' WHERE status NOT IN ('blocked', 'active', 'suspended')")
    op.create_check_constraint("ck_users_status", "users", _status_check(_OLD))
