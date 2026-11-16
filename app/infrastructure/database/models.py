import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP

from app.domain.entities.user import UserStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Tipos escolhidos para renderizar nativamente no PostgreSQL sem impedir que a
# mesma metadata rode em SQLite (usado na suíte de testes de repositório):
#   Uuid  -> UUID nativo no PG, CHAR(32) nos demais
#   _Inet -> INET nativo no PG, VARCHAR(45) nos demais (cabe IPv6 + zona)
_Inet = String(45).with_variant(INET(), "postgresql")

_USER_STATUSES = tuple(s.value for s in UserStatus)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    pass


# Permissions are a set per user and per key; each one belongs to an application, so the
# same account can be granted access to this API and to other interfaces that trust it.
user_permissions = Table(
    "user_permissions",
    Base.metadata,
    Column("user_id", Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Uuid(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
    Column("granted_at", TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()),
    Column("granted_by", Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
)

api_client_permissions = Table(
    "api_client_permissions",
    Base.metadata,
    Column("client_id", Uuid(as_uuid=True), ForeignKey("api_clients.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Uuid(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
    Column("granted_at", TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()),
)


class ApplicationModel(Base):
    """An interface that authenticates its users through this API; its slug is the token audience."""

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    permissions: Mapped[list["PermissionModel"]] = relationship(
        "PermissionModel", back_populates="application", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"ApplicationModel(id={self.id!r}, slug={self.slug!r})"


class PermissionModel(Base):
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("application_id", "name", name="uq_permissions_application_id_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    application: Mapped["ApplicationModel"] = relationship(
        "ApplicationModel", back_populates="permissions", lazy="joined"
    )

    @property
    def key(self) -> str:
        return f"{self.application.slug}:{self.name}"

    def __repr__(self) -> str:
        return f"PermissionModel(id={self.id!r}, name={self.name!r})"


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in_list_sql("status", _USER_STATUSES), name="ck_users_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=UserStatus.BLOCKED.value,
        server_default=UserStatus.BLOCKED.value,
        index=True,
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    provisional_password_sent_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # AES-256-GCM ciphertext; the key lives outside the database (MFA_ENCRYPTION_KEY).
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    permissions: Mapped[list["PermissionModel"]] = relationship(
        "PermissionModel",
        secondary=user_permissions,
        primaryjoin=lambda: UserModel.id == user_permissions.c.user_id,
        secondaryjoin=lambda: PermissionModel.id == user_permissions.c.permission_id,
        lazy="selectin",
    )
    refresh_tokens: Mapped[list["RefreshTokenModel"]] = relationship(
        "RefreshTokenModel", back_populates="user", cascade="all, delete-orphan"
    )
    email_tokens: Mapped[list["EmailTokenModel"]] = relationship(
        "EmailTokenModel", cascade="all, delete-orphan", passive_deletes=True
    )
    recovery_codes: Mapped[list["RecoveryCodeModel"]] = relationship(
        "RecoveryCodeModel", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # sem password_hash — não vaza credencial em log
        return (
            f"UserModel(id={self.id!r}, username={self.username!r}, "
            f"status={self.status!r})"
        )


class ApiClientModel(Base):
    """
    Aplicação consumidora autenticada por API key.

    `key_hash` é SHA-256 da chave completa, e não Argon2id: a chave é gerada
    com 256 bits de entropia, então não há ataque de dicionário a mitigar, e o
    hash sem salt permite buscar o cliente por índice em vez de varrer a tabela
    verificando um a um a cada request.
    """

    __tablename__ = "api_clients"
    __table_args__ = (
        # A name is unique among the active keys of one owner, and among active service keys.
        Index(
            "uq_api_clients_owner_name", "user_id", "name", unique=True,
            postgresql_where=text("user_id IS NOT NULL AND NOT revoked"),
            sqlite_where=text("user_id IS NOT NULL AND NOT revoked"),
        ),
        Index(
            "uq_api_clients_service_name", "name", unique=True,
            postgresql_where=text("user_id IS NULL AND NOT revoked"),
            sqlite_where=text("user_id IS NULL AND NOT revoked"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # First characters of the key ("trem_xxxx"): enough to recognise it in a list.
    key_hint: Mapped[str | None] = mapped_column(String(12), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    permissions: Mapped[list["PermissionModel"]] = relationship(
        "PermissionModel", secondary=api_client_permissions, lazy="selectin"
    )

    def __repr__(self) -> str:  # sem key_hash — é material sensível
        return (
            f"ApiClientModel(id={self.id!r}, name={self.name!r}, "
            f"revoked={self.revoked!r})"
        )


class RecoveryCodeModel(Base):
    __tablename__ = "mfa_recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    def __repr__(self) -> str:  # sem code_hash
        return f"RecoveryCodeModel(id={self.id!r}, used={self.used_at is not None})"


class EmailTokenModel(Base):
    """Single-use token sent by e-mail; only its SHA-256 is stored."""

    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    def __repr__(self) -> str:  # sem token_hash
        return f"EmailTokenModel(id={self.id!r}, purpose={self.purpose!r})"


class RefreshTokenModel(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Cobre revoke_all_refresh_tokens / varredura de tokens vivos por usuário
        Index("ix_refresh_tokens_user_id_revoked", "user_id", "revoked"),
        # Cobre a limpeza periódica de tokens expirados
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    ip_address: Mapped[str | None] = mapped_column(_Inet, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Every token of one login shares the family: reuse of a rotated token revokes all of them.
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    # How the login was proven ("pwd" or "pwd otp"); refreshed tokens keep it.
    amr: Mapped[str] = mapped_column(String(20), nullable=False, default="pwd", server_default="pwd")

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="refresh_tokens")
    application: Mapped["ApplicationModel"] = relationship("ApplicationModel", lazy="joined")

    def __repr__(self) -> str:  # sem token_hash — é material sensível
        return (
            f"RefreshTokenModel(id={self.id!r}, user_id={self.user_id!r}, "
            f"revoked={self.revoked!r})"
        )
