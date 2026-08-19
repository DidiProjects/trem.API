from typing import Callable, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    TokenExpiredError,
    TokenInvalidError,
)
from app.core.security import decode_token
from app.domain.entities.api_client import Principal
from app.domain.entities.user import User
from app.infrastructure.database.connection import get_db
from app.repositories.user_repository import UserRepository
from app.services.api_client_service import ApiClientService

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação não fornecido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials)
    except TokenExpiredError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado")
    except TokenInvalidError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")

    repo = UserRepository(db)
    user = await repo.get_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário não encontrado")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuário bloqueado ou suspenso",
        )

    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    """Bloqueia acesso se a senha não foi alterada após o primeiro login."""
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Alteração de senha obrigatória. Use POST /auth/change-password.",
        )
    return user


async def authenticated_user_or_none(
    api_key: Optional[str] = Security(_api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """
    Resolve o usuário do Bearer, ou None quando não há Bearer utilizável.

    Ignora o Bearer de propósito quando há X-API-Key: o FastAPI resolve todas
    as dependências antes do corpo da rota, então sem esse curto-circuito um
    header Authorization velho (deixado pelo browser ou injetado por proxy)
    derrubaria com 401 uma chamada que autentica por chave.

    Fica como dependência própria — e não inline em get_current_principal —
    para que a resolução do usuário seja substituível via dependency_overrides
    sem apagar as regras de perfil e senha pendente, que continuam rodando.
    """
    if api_key:
        return None
    if not credentials:
        return None
    return await get_current_user(credentials=credentials, db=db)


async def get_current_principal(
    api_key: Optional[str] = Security(_api_key_header),
    user: Optional[User] = Depends(authenticated_user_or_none),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """
    Resolve quem chamou, aceitando as duas formas de credencial:

      X-API-Key      -> aplicação consumidora (outra API, script, playground)
      Bearer <JWT>   -> usuário humano

    A API key é checada primeiro por ser o caminho majoritário hoje. O
    resultado é normalizado em Principal para que as rotas — e o
    require_profile — não precisem saber qual foi usada.
    """
    if api_key:
        client = await ApiClientService(db).authenticate(api_key)
        if not client:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API Key inválida ou revogada",
            )
        return Principal.from_client(client)

    if user:
        if user.must_change_password:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Alteração de senha obrigatória. Use POST /auth/change-password.",
            )
        return Principal.from_user(user)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credencial não fornecida. Envie X-API-Key ou Authorization: Bearer.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_profile(profile_name: str) -> Callable:
    """
    Factory: exige um perfil específico, seja o chamador usuário ou cliente.

    O perfil vem sempre do banco (registro do usuário ou do cliente), nunca de
    algo que o chamador possa forjar no request.
    """
    async def _check(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.profile_name != profile_name:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permissão insuficiente para este recurso",
            )
        return principal
    return _check
