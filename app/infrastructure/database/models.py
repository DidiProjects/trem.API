import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP

# Uuid renderiza UUID nativo no PostgreSQL e CHAR(32) nos demais: a mesma metadata roda
# em SQLite (usado na suíte de testes de repositório).


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    """A person who logs in through authentik, linked by username. authentik keeps the
    password, second factor and whether the account is active; this row holds what
    trem.API needs: the permissions (as of the last call) that cap the person's API keys."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
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

    permissions: Mapped[list["PermissionModel"]] = relationship(
        "PermissionModel",
        secondary=user_permissions,
        primaryjoin=lambda: UserModel.id == user_permissions.c.user_id,
        secondaryjoin=lambda: PermissionModel.id == user_permissions.c.permission_id,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"UserModel(id={self.id!r}, username={self.username!r})"


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


class HubServiceModel(Base):
    """A self-hosted service in the trem.FRONT catalog. Its permission hub:<slug> says who sees it."""

    __tablename__ = "hub_services"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null until the admin sets it: the catalog leaves the service out meanwhile.
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    logo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    logo_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Start of the logo's SHA-256: goes in the logo URL, so a new logo is a new URL and the
    # old one can be cached for good.
    logo_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=func.now()
    )

    permission: Mapped["PermissionModel"] = relationship("PermissionModel", lazy="joined")

    @property
    def slug(self) -> str:
        return self.permission.name

    def __repr__(self) -> str:  # without the logo bytes
        return f"HubServiceModel(id={self.id!r}, name={self.name!r})"
