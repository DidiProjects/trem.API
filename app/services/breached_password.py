from app.core.config import get_settings
from app.core.exceptions import WeakPasswordError
from app.infrastructure.http.pwned_passwords_client import get_pwned_passwords_client

BREACHED_MESSAGE = "Esta senha aparece em vazamentos de dados conhecidos. Escolha outra."


async def ensure_not_breached(password: str) -> None:
    """
    Refuses a password found in known breaches (NIST SP 800-63B, 5.1.1.2). If the service
    is down the password is accepted: an outage at a third party must not stop sign-ups
    and resets, and the length rule still holds.
    """
    if not get_settings().PWNED_PASSWORDS_ENABLED:
        return
    if await get_pwned_passwords_client().times_seen(password):
        raise WeakPasswordError(BREACHED_MESSAGE)
