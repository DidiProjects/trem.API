class AppException(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class InvalidCredentialsError(AppException):
    def __init__(self, message: str = "Credenciais inválidas"):
        super().__init__(message, status_code=401)


class TokenExpiredError(AppException):
    def __init__(self):
        super().__init__("Token expirado", status_code=401)


class TokenInvalidError(AppException):
    def __init__(self):
        super().__init__("Token inválido", status_code=401)


class UserBlockedError(AppException):
    def __init__(self):
        super().__init__(
            "Usuário bloqueado. Aguarde habilitação pelo administrador.",
            status_code=403,
        )


class EmailNotVerifiedError(AppException):
    def __init__(self):
        super().__init__("Confirme seu e-mail pelo link enviado antes de entrar.", status_code=403)


class AwaitingApprovalError(AppException):
    def __init__(self):
        super().__init__("Cadastro aguardando aprovação do administrador.", status_code=403)


class PasswordChangeRequiredError(AppException):
    def __init__(self):
        super().__init__(
            "Alteração de senha obrigatória antes de continuar.",
            status_code=403,
        )


class UserNotFoundError(AppException):
    def __init__(self):
        super().__init__("Usuário não encontrado", status_code=404)


class UserAlreadyExistsError(AppException):
    def __init__(self):
        super().__init__("Nome de usuário já cadastrado", status_code=409)


class UnknownPermissionsError(AppException):
    def __init__(self, keys):
        super().__init__(f"Permissões não encontradas: {', '.join(sorted(keys))}", status_code=400)


class RateLimitedError(AppException):
    def __init__(self, retry_after: int, message: str = "Muitas tentativas. Tente novamente mais tarde."):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class TooManyLoginAttemptsError(RateLimitedError):
    def __init__(self, retry_after: int):
        super().__init__(retry_after, "Muitas tentativas de login. Tente novamente mais tarde.")


class SignupDisabledError(AppException):
    def __init__(self):
        super().__init__("Cadastro desativado", status_code=403)


class InvalidEmailTokenError(AppException):
    def __init__(self):
        super().__init__("Link inválido ou expirado", status_code=400)


class WeakPasswordError(AppException):
    def __init__(self, message: str):
        super().__init__(message, 422)


class ApplicationAccessDeniedError(AppException):
    def __init__(self):
        super().__init__("Sem acesso a esta aplicação", status_code=403)


class MfaNotConfiguredError(AppException):
    def __init__(self):
        super().__init__("Segundo fator indisponível neste servidor", status_code=503)


class InvalidMfaCodeError(AppException):
    def __init__(self):
        super().__init__("Código inválido", status_code=401)


class MfaStateError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class ConflictError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class NotFoundError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=404)


class InsufficientPermissionsError(AppException):
    def __init__(self):
        super().__init__("Permissão insuficiente para este recurso", status_code=403)


class EmailSendError(AppException):
    def __init__(self, detail: str = ""):
        msg = "Erro ao enviar email"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg, status_code=503)


class InvalidLogoError(AppException):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)
