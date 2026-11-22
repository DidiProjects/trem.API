from fastapi import HTTPException

from app.core.exceptions import AppException, RateLimitedError


def http_error(e: AppException) -> HTTPException:
    headers = None
    if isinstance(e, RateLimitedError):
        headers = {"Retry-After": str(e.retry_after)}
    return HTTPException(status_code=e.status_code, detail=e.message, headers=headers)
