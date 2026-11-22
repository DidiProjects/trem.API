from typing import Callable, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    TokenExpiredError,
    TokenInvalidError,
)
from app.core.authentik import AuthentikUnavailableError, decode_authentik_token
from app.domain.entities.api_client import Principal
from app.domain.entities.permission import split_permission_key
from app.domain.entities.user import User
from app.infrastructure.database.connection import get_db
from app.services.api_client_service import ApiClientService
from app.services.authentik_user_service import AuthentikUserService

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> User:
    """The person behind an access token issued by authentik (trem.AUTH): the only login."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação não fornecido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = await decode_authentik_token(credentials.credentials)
        user = await AuthentikUserService(db).resolve(claims)
    except TokenExpiredError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado")
    except TokenInvalidError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    except AuthentikUnavailableError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.message)
    return user


async def authenticated_user_or_none(
    api_key: Optional[str] = Security(_api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> Optional[User]:
    """
    Resolve o usuário do Bearer, ou None quando não há Bearer utilizável.

    Ignora o Bearer de propósito quando há X-API-Key: o FastAPI resolve todas
    as dependências antes do corpo da rota, então sem esse curto-circuito um
    header Authorization velho (deixado pelo browser ou injetado por proxy)
    derrubaria com 401 uma chamada que autentica por chave.

    Fica como dependência própria — e não inline em get_current_principal —
    para que a resolução do usuário seja substituível via dependency_overrides
    sem apagar as regras de perfil, que continuam rodando.
    """
    if api_key:
        return None
    if not credentials:
        return None
    return await get_current_user(credentials=credentials, db=db)


async def get_current_principal(
    api_key: Optional[str] = Security(_api_key_header),
    user: Optional[User] = Depends(authenticated_user_or_none),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> Principal:
    """
    Resolve quem chamou, aceitando as duas formas de credencial:

      X-API-Key      -> aplicação consumidora (outra API, script, playground)
      Bearer <token do authentik> -> usuário humano

    A API key é checada primeiro por ser o caminho majoritário hoje. O
    resultado é normalizado em Principal para que as rotas — e o
    require_permission — não precisem saber qual foi usada.
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
        return Principal.from_user(user)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credencial não fornecida. Envie X-API-Key ou Authorization: Bearer.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """
    Administration needs a person, not a key: an authentik login holding trem:admin, proven
    with the second factor. A leaked password or API key alone cannot reach it.
    """
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permissão insuficiente para este recurso")
    if "otp" not in user.amr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administração exige login com segundo fator (TOTP)",
        )
    return user


def require_permission(key: str) -> Callable:
    """
    Factory: exige uma permissão "<aplicação>:<permissão>", seja o chamador usuário ou cliente.

    Para uma pessoa, a permissão vem dos grupos do token do authentik, assinados por ele e
    válidos por 10 minutos. Para uma chave, vem do banco (registro do cliente, limitado
    às permissões gravadas do dono).
    """
    split_permission_key(key)

    async def _check(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_permission(key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permissão insuficiente para este recurso",
            )
        return principal
    return _check
