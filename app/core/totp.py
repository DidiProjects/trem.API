"""
TOTP (RFC 6238, SHA-1, 6 digits, 30 s) and the encryption of its secret at rest.

The algorithm is small enough to own, and owning it keeps the parameters that every
authenticator app expects in one visible place.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STEP_SECONDS = 30
DIGITS = 6
# One step of drift each way: phones and servers disagree by a few seconds.
DRIFT_STEPS = 1


def generate_secret() -> str:
    """160-bit secret, base32 as authenticator apps read it."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def code_at(secret: str, now: float) -> str:
    return _code(secret, current_step(now))


def verify(secret: str, code: str, last_step: Optional[int], now: Optional[float] = None) -> Optional[int]:
    """
    Returns the matched time step, or None. A step at or before `last_step` is refused:
    a code seen once (shoulder-surfed, phished, replayed) cannot be used again.
    """
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - DRIFT_STEPS, step + DRIFT_STEPS + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(_code(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "trem.API") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"


def _key(encoded_key: str) -> bytes:
    key = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
    if len(key) != 32:
        raise ValueError("MFA_ENCRYPTION_KEY must be 32 bytes, base64url-encoded")
    return key


def encrypt_secret(secret: str, encoded_key: str, user_id: str) -> str:
    """
    AES-256-GCM. The user id is bound as associated data: a ciphertext copied onto
    another account does not decrypt there.
    """
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_key(encoded_key)).encrypt(nonce, secret.encode(), user_id.encode())
    return base64.b64encode(nonce + sealed).decode()


def decrypt_secret(token: str, encoded_key: str, user_id: str) -> str:
    raw = base64.b64decode(token)
    return AESGCM(_key(encoded_key)).decrypt(raw[:12], raw[12:], user_id.encode()).decode()


def generate_recovery_codes(count: int = 10) -> list[str]:
    """xxxxx-xxxxx from an alphabet without look-alikes; ~50 bits each, single use."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(2))
        for _ in range(count)
    ]


def normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")
