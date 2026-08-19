import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP

from app.domain.entities.profile import ProfileName
from app.domain.entities.user import UserStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Tipos escolhidos para renderizar nativamente no PostgreSQL sem impedir que a
# mesma metadata rode em SQLite (usado na suíte de testes de repositório):
#   Uuid  -> UUID nativo no PG, CHAR(32) nos demais
#   _Inet -> INET nativo no PG, VARCHAR(45) nos demais (cabe IPv6 + zona)
_Inet = String(45).with_variant(INET(), "postgresql")

_USER_STATUSES = tuple(s.value for s in UserStatus)
_PROFILE_NAMES = tuple(p.value for p in ProfileName)


def _in_list_sql(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    pass


class ProfileModel(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint(_in_list_sql("name", _PROFILE_NAMES), name="ck_profiles_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_now, server_default=func.now()
    )

    users: Mapped[list["UserModel"]] = relationship("UserModel", back_populates="profile")

    def __repr__(self) -> str:
        return f"ProfileModel(id={self.id!r}, name={self.name!r})"


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
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("profiles.id"), nullable=False
    )
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

    profile: Mapped["ProfileModel"] = relationship("ProfileModel", back_populates="users")
    refresh_tokens: Mapped[list["RefreshTokenModel"]] = relationship(
        "RefreshTokenModel", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # sem password_hash — não vaza credencial em log
        return (
            f"UserModel(id={self.id!r}, username={self.username!r}, "
            f"status={self.status!r})"
        )


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

    user: Mapped["UserModel"] = relationship("UserModel", back_populates="refresh_tokens")

    def __repr__(self) -> str:  # sem token_hash — é material sensível
        return (
            f"RefreshTokenModel(id={self.id!r}, user_id={self.user_id!r}, "
            f"revoked={self.revoked!r})"
        )
