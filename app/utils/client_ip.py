from fastapi import Request


def client_ip(request: Request) -> str:
    """
    The caller's address behind Cloudflare. Cloudflare overwrites CF-Connecting-IP,
    while the first X-Forwarded-For entry is whatever the client chose to send, so it
    cannot key a rate limit. Without the header the request came from the host itself.
    """
    forwarded = request.headers.get("CF-Connecting-IP")
    if forwarded:
        return forwarded.strip()
    return request.client.host if request.client else "unknown"
