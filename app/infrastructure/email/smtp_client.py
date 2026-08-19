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

    async def _send(self, to: str, subject: str, body: str) -> None:
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
