from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routers.auth import http_error
from app.core.exceptions import AppException
from app.domain.schemas.auth import EmailTokenRequest, RegisterRequest, ResendVerificationRequest
from app.domain.schemas.common import MessageResponse
from app.infrastructure.database.connection import get_db
from app.infrastructure.email.smtp_client import SmtpEmailClient
from app.services.signup_service import SignupService, deliver
from app.utils.client_ip import client_ip

router = APIRouter(prefix="/auth", tags=["Auth"])

_ACCEPTED = (
    "Se os dados estiverem corretos, enviamos um e-mail de confirmação. "
    "Depois de confirmado, o cadastro segue para aprovação."
)


def get_email_client():
    return SmtpEmailClient()


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def register(
    body: RegisterRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function"),
    email_client=Depends(get_email_client),
):
    service = SignupService(db, email_client)
    try:
        await service.register(body.username, body.email, body.password, client_ip(request))
    except AppException as e:
        raise http_error(e)
    background.add_task(deliver, service.outbox)
    return MessageResponse(message=_ACCEPTED)


@router.post("/resend-verification", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(
    body: ResendVerificationRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function"),
    email_client=Depends(get_email_client),
):
    service = SignupService(db, email_client)
    try:
        await service.resend_verification(body.email, client_ip(request))
    except AppException as e:
        raise http_error(e)
    background.add_task(deliver, service.outbox)
    return MessageResponse(message="Se houver um cadastro pendente com este e-mail, enviamos um novo link.")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    body: EmailTokenRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db, scope="function"),
    email_client=Depends(get_email_client),
):
    service = SignupService(db, email_client)
    try:
        await service.verify_email(body.token)
    except AppException as e:
        raise http_error(e)
    background.add_task(deliver, service.outbox)
    return MessageResponse(message="E-mail confirmado. O cadastro aguarda aprovação do administrador.")
