"""
Testes dos dois caminhos de email da API:

  - app/services/emailService.py       — feedback (config via os.environ)
  - app/infrastructure/email/smtp_client.py — senha provisória + feedback (config via Settings)

smtplib.SMTP é sempre mockado: nenhum teste abre conexão de rede.
"""
import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app.core.exceptions import EmailSendError
from app.infrastructure.email.smtp_client import SmtpEmailClient
from app.services.emailService import (
    EmailServiceError,
    get_email_config,
    send_feedback_email,
    validate_email_config,
)

SMTP_ENV = {
    "EMAIL_RECIPIENT": "destino@example.com",
    "EMAIL_SMTP_HOST": "smtp.example.com",
    "EMAIL_SMTP_PORT": "587",
    "EMAIL_SMTP_USER": "remetente@example.com",
    "EMAIL_SMTP_PASSWORD": "senha-smtp",
}


@pytest.fixture
def smtp_server():
    """Mocka smtplib.SMTP e devolve o objeto servidor usado dentro do `with`."""
    with patch("smtplib.SMTP") as smtp_cls:
        server = MagicMock()
        smtp_cls.return_value.__enter__.return_value = server
        yield server


# ---------------------------------------------------------------------------
# emailService (feedback)
# ---------------------------------------------------------------------------

class TestGetEmailConfig:
    def test_reads_environment(self, monkeypatch):
        for k, v in SMTP_ENV.items():
            monkeypatch.setenv(k, v)

        config = get_email_config()
        assert config["recipient"] == "destino@example.com"
        assert config["smtp_host"] == "smtp.example.com"
        assert config["smtp_port"] == 587

    def test_defaults_to_gmail_host(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SMTP_HOST", raising=False)
        assert get_email_config()["smtp_host"] == "smtp.gmail.com"

    def test_default_port_is_587(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SMTP_PORT", raising=False)
        assert get_email_config()["smtp_port"] == 587


class TestValidateEmailConfig:
    def test_complete_config_is_valid(self):
        assert validate_email_config(
            {"recipient": "a@b.com", "smtp_user": "u", "smtp_password": "p"}
        ) is True

    @pytest.mark.parametrize("missing", ["recipient", "smtp_user", "smtp_password"])
    def test_missing_field_is_invalid(self, missing):
        config = {"recipient": "a@b.com", "smtp_user": "u", "smtp_password": "p"}
        config[missing] = None
        assert validate_email_config(config) is False

    def test_empty_string_is_invalid(self):
        assert validate_email_config(
            {"recipient": "", "smtp_user": "u", "smtp_password": "p"}
        ) is False


class TestSendFeedbackEmail:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        for k, v in SMTP_ENV.items():
            monkeypatch.setenv(k, v)

    def test_success_returns_true(self, smtp_server):
        assert send_feedback_email("bug", "Encontrei um problema") is True

    def test_success_authenticates_and_sends(self, smtp_server):
        send_feedback_email("bug", "Encontrei um problema")
        smtp_server.starttls.assert_called_once()
        smtp_server.login.assert_called_once_with("remetente@example.com", "senha-smtp")
        smtp_server.send_message.assert_called_once()

    def test_message_body_contains_feedback(self, smtp_server):
        send_feedback_email("bug", "O botão X não funciona")
        sent = smtp_server.send_message.call_args[0][0]
        assert "O botão X não funciona" in sent.get_payload()[0].get_payload(decode=True).decode()

    @pytest.mark.parametrize(
        "tipo,label",
        [
            ("suggestion", "Sugestão de Funcionalidade"),
            ("bug", "Reporte de Bug"),
            ("other", "Outro Feedback"),
        ],
    )
    def test_subject_reflects_type(self, smtp_server, tipo, label):
        send_feedback_email(tipo, "mensagem")
        assert label in smtp_server.send_message.call_args[0][0]["Subject"]

    def test_includes_reply_email_when_given(self, smtp_server):
        send_feedback_email("bug", "mensagem", user_email="quem@example.com")
        sent = smtp_server.send_message.call_args[0][0]
        body = sent.get_payload()[0].get_payload(decode=True).decode()
        assert "quem@example.com" in body

    def test_notes_absence_of_reply_email(self, smtp_server):
        send_feedback_email("bug", "mensagem")
        sent = smtp_server.send_message.call_args[0][0]
        body = sent.get_payload()[0].get_payload(decode=True).decode()
        assert "Sem email informado" in body

    def test_missing_config_raises_503(self, monkeypatch):
        monkeypatch.delenv("EMAIL_SMTP_USER", raising=False)
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "mensagem")
        assert exc.value.status_code == 503

    def test_empty_message_raises_400(self, smtp_server):
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "   ")
        assert exc.value.status_code == 400

    def test_too_long_message_raises_400(self, smtp_server):
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "x" * 5001)
        assert exc.value.status_code == 400

    def test_message_at_size_limit_is_accepted(self, smtp_server):
        assert send_feedback_email("bug", "x" * 5000) is True

    def test_auth_failure_raises_503(self, smtp_server):
        smtp_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "mensagem")
        assert exc.value.status_code == 503

    def test_smtp_failure_raises_503(self, smtp_server):
        smtp_server.send_message.side_effect = smtplib.SMTPException("boom")
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "mensagem")
        assert exc.value.status_code == 503

    def test_unexpected_failure_raises_500(self, smtp_server):
        smtp_server.send_message.side_effect = RuntimeError("inesperado")
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "mensagem")
        assert exc.value.status_code == 500

    def test_does_not_leak_password_in_error_message(self, smtp_server):
        smtp_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
        with pytest.raises(EmailServiceError) as exc:
            send_feedback_email("bug", "mensagem")
        assert "senha-smtp" not in str(exc.value)


# ---------------------------------------------------------------------------
# SmtpEmailClient (senha provisória)
# ---------------------------------------------------------------------------

def _settings_with(**overrides):
    from app.core.config import Settings, get_settings

    data = get_settings().model_dump()
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def configured_settings():
    settings = _settings_with(
        EMAIL_SMTP_HOST="smtp.example.com",
        EMAIL_SMTP_PORT=587,
        EMAIL_SMTP_USER="remetente@example.com",
        EMAIL_SMTP_PASSWORD="senha-smtp",
        EMAIL_RECIPIENT="destino@example.com",
    )
    with patch(
        "app.infrastructure.email.smtp_client.get_settings", return_value=settings
    ):
        yield settings


class TestSmtpEmailClientProvisionalPassword:
    async def test_sends_message(self, smtp_server, configured_settings):
        await SmtpEmailClient().send_provisional_password(
            to="usuario@example.com", username="joao", provisional_password="Prov@123"
        )
        smtp_server.send_message.assert_called_once()

    async def test_addresses_the_user(self, smtp_server, configured_settings):
        await SmtpEmailClient().send_provisional_password(
            to="usuario@example.com", username="joao", provisional_password="Prov@123"
        )
        assert smtp_server.send_message.call_args[0][0]["To"] == "usuario@example.com"

    async def test_body_contains_credentials(self, smtp_server, configured_settings):
        await SmtpEmailClient().send_provisional_password(
            to="usuario@example.com", username="joao", provisional_password="Prov@123"
        )
        sent = smtp_server.send_message.call_args[0][0]
        body = sent.get_payload()[0].get_payload(decode=True).decode()
        assert "joao" in body
        assert "Prov@123" in body

    async def test_without_credentials_raises(self, smtp_server):
        unconfigured = _settings_with(EMAIL_SMTP_USER="", EMAIL_SMTP_PASSWORD="")
        with patch(
            "app.infrastructure.email.smtp_client.get_settings",
            return_value=unconfigured,
        ):
            with pytest.raises(EmailSendError):
                await SmtpEmailClient().send_provisional_password(
                    to="u@example.com", username="joao", provisional_password="Prov@123"
                )

    async def test_auth_failure_raises_email_send_error(
        self, smtp_server, configured_settings
    ):
        smtp_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
        with pytest.raises(EmailSendError) as exc:
            await SmtpEmailClient().send_provisional_password(
                to="u@example.com", username="joao", provisional_password="Prov@123"
            )
        assert exc.value.status_code == 503


class TestSmtpEmailClientFeedback:
    async def test_sends_to_configured_recipient(self, smtp_server, configured_settings):
        await SmtpEmailClient().send_feedback("bug", "algo quebrou")
        assert smtp_server.send_message.call_args[0][0]["To"] == "destino@example.com"

    async def test_without_recipient_raises(self, smtp_server):
        no_recipient = _settings_with(
            EMAIL_SMTP_USER="u@example.com",
            EMAIL_SMTP_PASSWORD="p",
            EMAIL_RECIPIENT=None,
        )
        with patch(
            "app.infrastructure.email.smtp_client.get_settings", return_value=no_recipient
        ):
            with pytest.raises(EmailSendError):
                await SmtpEmailClient().send_feedback("bug", "algo quebrou")

    async def test_includes_user_email_when_given(self, smtp_server, configured_settings):
        await SmtpEmailClient().send_feedback("bug", "algo quebrou", "quem@example.com")
        sent = smtp_server.send_message.call_args[0][0]
        body = sent.get_payload()[0].get_payload(decode=True).decode()
        assert "quem@example.com" in body
