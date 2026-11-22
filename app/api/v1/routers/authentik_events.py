"""Account changes pushed by authentik (trem.AUTH account-events.yaml)."""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infrastructure.database.connection import get_db
from app.services.authentik_user_service import AuthentikUserService

router = APIRouter(prefix="/internal/authentik", include_in_schema=False)


class AccountEvent(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    active: bool
    groups: list[str] = Field(default_factory=list, max_length=500)


def _check_secret(authorization: str | None = Header(None)) -> None:
    secret = get_settings().AUTHENTIK_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credencial inválida")


@router.post("/accounts", dependencies=[Depends(_check_secret)])
async def account_changed(body: AccountEvent, db: AsyncSession = Depends(get_db, scope="function")):
    permissions = await AuthentikUserService(db).sync_account(body.username, body.active, body.groups)
    return {"known": permissions is not None, "permissions": permissions or []}
