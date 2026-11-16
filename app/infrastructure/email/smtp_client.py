import asyncio
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import get_settings
from app.core.exceptions import EmailSendError
from app.core.interfaces.i_email_client import IEmailClient

logger = logging.getLogger(__name__)


class SmtpEmailClient(IEmailClient):

    async def send_provisional_password(
        self,
        to: str,
        username: str,
        provisional_password: str,
    ) -> None:
        settings = get_settings()
        subject = "[trem.API] Seu acesso foi criado"
        body = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bem-vindo(a) ao trem.API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Olá, {username}!

Seu acesso foi criado. Use as credenciais abaixo para fazer login:

  Usuário: {username}
  Senha:   {provisional_password}

⚠️  Você será obrigado(a) a alterar sua senha no primeiro acesso.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mensagem automática — não responda este email
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        await self._send(to=to, subject=subject, body=body)

    async def send_feedback(
        self,
        feedback_type: str,
        message: str,
        user_email: str | None = None,
    ) -> None:
        settings = get_settings()
        recipient = settings.EMAIL_RECIPIENT
        if not recipient:
            raise EmailSendError("EMAIL_RECIPIENT não configurado")

        type_labels = {
            "suggestion": "Sugestão de Funcionalidade",
            "bug": "Reporte de Bug",
            "other": "Outro Feedback",
        }
        label = type_labels.get(feedback_type, "Feedback")
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        reply_info = f"Email para resposta: {user_email}" if user_email else "Sem email informado"

        body = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Data/Hora: {timestamp}
{reply_info}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MENSAGEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{message.strip()}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Enviado automaticamente via trem.API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        await self._send(to=recipient, subject=f"[trem.API] {label}", body=body)

    async def send_email_verification(self, to: str, username: str, link: str, ttl_hours: int) -> None:
        body = f"""Olá, {username}!

Recebemos um cadastro no trem.API com este e-mail. Para confirmar, abra:

{link}

O link vale por {ttl_hours} horas e funciona uma vez. Depois da confirmação, o
cadastro segue para aprovação do administrador.

Se não foi você, ignore esta mensagem: sem a confirmação, nenhuma conta é ativada.
"""
        await self._send(to=to, subject="[trem.API] Confirme seu e-mail", body=body)

    async def send_signup_conflict(self, to: str, reason: str, login_url: str) -> None:
        """Answers a sign-up that hit an existing account, without telling the requester anything."""
        lines = {
            "email": "Já existe uma conta com este e-mail. Se esqueceu a senha, use \"Esqueci a senha\" na tela de login.",
            "username": "O nome de usuário escolhido já está em uso. Faça o cadastro de novo com outro nome.",
        }
        body = f"""Recebemos um pedido de cadastro no trem.API com este e-mail.

{lines[reason]}

Entrar: {login_url}

Se não foi você, ignore esta mensagem.
"""
        await self._send(to=to, subject="[trem.API] Pedido de cadastro", body=body)

    async def send_signup_pending_approval(self, to: str, username: str, email: str, user_id: str) -> None:
        body = f"""Novo cadastro aguardando aprovação no trem.API.

  Usuário: {username}
  E-mail:  {email} (confirmado)
  ID:      {user_id}

Aprove e defina as permissões em POST /admin/users/{user_id}/approve,
ou recuse em POST /admin/users/{user_id}/reject.
"""
        await self._send(to=to, subject=f"[trem.API] Cadastro pendente: {username}", body=body)

    async def send_password_reset(self, to: str, username: str, link: str, ttl_minutes: int) -> None:
        body = f"""Olá, {username}!

Recebemos um pedido para redefinir a senha da sua conta no trem.API. Para criar
uma senha nova, abra:

{link}

O link vale por {ttl_minutes} minutos e funciona uma vez. Se a conta usa
autenticação em dois fatores, o código continua sendo pedido no login.

Se não foi você, ignore esta mensagem: a senha atual continua valendo.
"""
        await self._send(to=to, subject="[trem.API] Redefinição de senha", body=body)

    async def send_password_changed(self, to: str, username: str) -> None:
        body = f"""Olá, {username}!

A senha da sua conta no trem.API foi redefinida, e todas as sessões abertas
foram encerradas. As suas API keys continuam ativas.

Se não foi você, redefina a senha agora pela opção "Esqueci a senha" e avise o
administrador.
"""
        await self._send(to=to, subject="[trem.API] Sua senha foi alterada", body=body)

    async def _send(self, to: str, subject: str, body: str) -> None:
        # smtplib blocks: off the event loop, a slow SMTP server does not stall every request.
        await asyncio.to_thread(self._deliver, to, subject, body)

    def _deliver(self, to: str, subject: str, body: str) -> None:
        settings = get_settings()
        if not settings.EMAIL_SMTP_USER or not settings.EMAIL_SMTP_PASSWORD:
            raise EmailSendError("Credenciais SMTP não configuradas")

        msg = MIMEMultipart()
        msg["From"] = settings.EMAIL_SMTP_USER
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP(settings.EMAIL_SMTP_HOST, settings.EMAIL_SMTP_PORT) as server:
                server.starttls()
                server.login(settings.EMAIL_SMTP_USER, settings.EMAIL_SMTP_PASSWORD)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise EmailSendError("Falha na autenticação SMTP")
        except smtplib.SMTPException as e:
            raise EmailSendError(str(e))
        except Exception as e:
            logger.exception("Erro inesperado ao enviar email")
            raise EmailSendError(str(e))
