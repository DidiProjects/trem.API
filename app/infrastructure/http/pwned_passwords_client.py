import hashlib
import logging
from functools import lru_cache
from typing import Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class PwnedPasswordsClient:
    """
    Have I Been Pwned "range" API (k-anonymity): only the first 5 hex characters of the
    SHA-1 leave the server, and the match against the returned suffixes happens here.
    Add-Padding makes every answer about the same size, so the response length does not
    narrow down the prefix to someone watching the traffic.
    """

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        settings = get_settings()
        self._base_url = settings.PWNED_PASSWORDS_URL.rstrip("/")
        self._timeout = httpx.Timeout(settings.PWNED_PASSWORDS_TIMEOUT)
        self._transport = transport

    async def times_seen(self, password: str) -> Optional[int]:
        """How many breaches carry this password; None when the service could not answer."""
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.get(
                    f"{self._base_url}/range/{prefix}",
                    headers={"Add-Padding": "true", "User-Agent": "trem.API"},
                )
        except httpx.HTTPError as e:
            logger.warning("pwned passwords unavailable: %s", type(e).__name__)
            return None
        if resp.status_code != 200:
            logger.warning("pwned passwords answered %s", resp.status_code)
            return None

        for line in resp.text.splitlines():
            candidate, _, count = line.strip().partition(":")
            if candidate == suffix:
                # Padding entries come with a count of 0.
                return int(count or 0)
        return 0


@lru_cache()
def get_pwned_passwords_client() -> PwnedPasswordsClient:
    return PwnedPasswordsClient()
