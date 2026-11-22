class AppException(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TokenExpiredError(AppException):
    def __init__(self):
        super().__init__("Token expirado", status_code=401)


class TokenInvalidError(AppException):
    def __init__(self):
        super().__init__("Token inválido", status_code=401)


class UnknownPermissionsError(AppException):
    def __init__(self, keys):
        super().__init__(f"Permissões não encontradas: {', '.join(sorted(keys))}", status_code=400)


class RateLimitedError(AppException):
    def __init__(self, retry_after: int, message: str = "Muitas tentativas. Tente novamente mais tarde."):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


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
