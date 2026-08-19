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


class InsufficientPermissionsError(AppException):
    def __init__(self):
        super().__init__("Permissão insuficiente para este recurso", status_code=403)


class EmailSendError(AppException):
    def __init__(self, detail: str = ""):
        msg = "Erro ao enviar email"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg, status_code=503)
