"""
Testes de app/core/security.py — hashing de senha (Argon2id) e JWT RS256.

É o módulo mais sensível da API: qualquer regressão aqui vira falha de
autenticação silenciosa, então cobre também os cenários de ataque
(token adulterado, assinatura de outra chave, confusão de algoritmo).
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings, get_settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_provisional_password,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from tests.conftest import TEST_PRIVATE_KEY, TEST_PUBLIC_KEY

USER_ID = "11111111-1111-1111-1111-111111111111"


def _settings_with(**overrides) -> Settings:
    base = get_settings()
    data = base.model_dump()
    data.update(overrides)
    return Settings(**data)


class TestHashPassword:
    def test_produces_argon2id_hash(self):
        assert hash_password("SenhaForte@123").startswith("$argon2id$")

    def test_same_password_gives_different_hashes(self):
        """Salt aleatório: dois hashes da mesma senha nunca podem coincidir."""
        assert hash_password("SenhaForte@123") != hash_password("SenhaForte@123")

    def test_hash_does_not_contain_plaintext(self):
        assert "SenhaForte@123" not in hash_password("SenhaForte@123")

    def test_accepts_unicode_password(self):
        pwd = "señhaç~ão@123ÁÉÍ"
        assert verify_password(pwd, hash_password(pwd)) is True

    def test_accepts_long_password(self):
        pwd = "A1" * 200
        assert verify_password(pwd, hash_password(pwd)) is True


class TestVerifyPassword:
    def test_correct_password(self):
        assert verify_password("SenhaForte@123", hash_password("SenhaForte@123")) is True

    def test_wrong_password(self):
        assert verify_password("errada", hash_password("SenhaForte@123")) is False

    def test_case_sensitive(self):
        assert verify_password("senhaforte@123", hash_password("SenhaForte@123")) is False

    def test_empty_password_against_real_hash(self):
        assert verify_password("", hash_password("SenhaForte@123")) is False

    def test_malformed_hash_returns_false_instead_of_raising(self):
        """Hash corrompido no banco não pode derrubar o login com exceção."""
        assert verify_password("qualquer", "nao-e-um-hash-argon2") is False

    def test_empty_hash_returns_false(self):
        assert verify_password("qualquer", "") is False

    def test_bcrypt_hash_returns_false(self):
        legacy = "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRS"
        assert verify_password("qualquer", legacy) is False


class TestNeedsRehash:
    def test_fresh_hash_does_not_need_rehash(self):
        assert needs_rehash(hash_password("SenhaForte@123")) is False

    def test_weaker_parameters_need_rehash(self):
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("x")
        assert needs_rehash(weak) is True


class TestGenerateProvisionalPassword:
    def test_default_length(self):
        assert len(generate_provisional_password()) == 14

    def test_custom_length(self):
        assert len(generate_provisional_password(20)) == 20

    def test_has_upper_lower_digit_and_symbol(self):
        pwd = generate_provisional_password()
        assert any(c.isupper() for c in pwd)
        assert any(c.islower() for c in pwd)
        assert any(c.isdigit() for c in pwd)
        assert any(c in "!@#$%" for c in pwd)

    def test_passes_change_password_strength_rules(self):
        """A senha provisória precisa ser aceita pelo schema de troca de senha."""
        from app.domain.schemas.auth import ChangePasswordRequest

        body = ChangePasswordRequest(
            current_password="anterior", new_password=generate_provisional_password()
        )
        assert body.new_password

    def test_is_random(self):
        generated = {generate_provisional_password() for _ in range(50)}
        assert len(generated) == 50

    def test_uses_only_allowed_alphabet(self):
        import string

        allowed = set(string.ascii_letters + string.digits + "!@#$%")
        assert set(generate_provisional_password(60)) <= allowed


class TestAccessToken:
    def test_roundtrip_claims(self):
        token = create_access_token(
            user_id=USER_ID,
            username="testuser",
            profile="file_editor",
            must_change_password=False,
        )
        payload = decode_token(token)

        assert payload["sub"] == USER_ID
        assert payload["username"] == "testuser"
        assert payload["profile"] == "file_editor"
        assert payload["must_change_password"] is False
        assert payload["type"] == "access"

    def test_must_change_password_flag_is_preserved(self):
        token = create_access_token(USER_ID, "u", "file_editor", True)
        assert decode_token(token)["must_change_password"] is True

    def test_has_exp_and_iat(self):
        payload = decode_token(create_access_token(USER_ID, "u", "file_editor", False))
        assert payload["exp"] > payload["iat"]

    def test_expires_according_to_settings(self):
        payload = decode_token(create_access_token(USER_ID, "u", "file_editor", False))
        lifetime = payload["exp"] - payload["iat"]
        expected = get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert abs(lifetime - expected) <= 1

    def test_is_signed_with_rs256(self):
        token = create_access_token(USER_ID, "u", "file_editor", False)
        assert pyjwt.get_unverified_header(token)["alg"] == "RS256"

    def test_token_is_a_string(self):
        assert isinstance(create_access_token(USER_ID, "u", "file_editor", False), str)


class TestRefreshToken:
    def test_returns_token_and_jti(self):
        token, jti = create_refresh_token(USER_ID)
        assert isinstance(token, str)
        assert decode_token(token)["jti"] == jti

    def test_type_is_refresh(self):
        token, _ = create_refresh_token(USER_ID)
        assert decode_token(token)["type"] == "refresh"

    def test_jti_is_unique_per_call(self):
        _, jti_a = create_refresh_token(USER_ID)
        _, jti_b = create_refresh_token(USER_ID)
        assert jti_a != jti_b

    def test_lives_longer_than_access_token(self):
        refresh, _ = create_refresh_token(USER_ID)
        access = create_access_token(USER_ID, "u", "file_editor", False)
        assert decode_token(refresh)["exp"] > decode_token(access)["exp"]

    def test_does_not_carry_profile_claims(self):
        """Refresh token só serve para renovar — não deve carregar autorização."""
        payload = decode_token(create_refresh_token(USER_ID)[0])
        assert "profile" not in payload
        assert "must_change_password" not in payload


class TestDecodeTokenRejections:
    def test_expired_token_raises_token_expired(self):
        expired = _settings_with(ACCESS_TOKEN_EXPIRE_MINUTES=-5)
        with patch("app.core.security.get_settings", return_value=expired):
            token = create_access_token(USER_ID, "u", "file_editor", False)
            with pytest.raises(TokenExpiredError):
                decode_token(token)

    def test_garbage_string_raises_token_invalid(self):
        with pytest.raises(TokenInvalidError):
            decode_token("isso-nao-e-um-jwt")

    def test_empty_string_raises_token_invalid(self):
        with pytest.raises(TokenInvalidError):
            decode_token("")

    def test_tampered_payload_raises_token_invalid(self):
        token = create_access_token(USER_ID, "u", "file_editor", False)
        header, payload, signature = token.split(".")

        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["profile"] = "airline_company"  # escalada de privilégio
        forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")

        with pytest.raises(TokenInvalidError):
            decode_token(f"{header}.{forged}.{signature}")

    def test_token_signed_by_another_key_raises_token_invalid(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        now = datetime.now(timezone.utc)
        forged = pyjwt.encode(
            {
                "sub": USER_ID,
                "type": "access",
                "iat": now,
                "exp": now + timedelta(minutes=10),
            },
            other_pem,
            algorithm="RS256",
        )
        with pytest.raises(TokenInvalidError):
            decode_token(forged)

    def test_alg_none_is_rejected(self):
        now = datetime.now(timezone.utc)
        unsigned = pyjwt.encode(
            {
                "sub": USER_ID,
                "type": "access",
                "iat": now,
                "exp": now + timedelta(minutes=10),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenInvalidError):
            decode_token(unsigned)

    def test_hs256_signed_with_public_key_is_rejected(self):
        """
        Confusão de algoritmo: o atacante troca alg para HS256 e assina usando a
        chave *pública* RSA como segredo HMAC. O token é montado à mão porque a
        própria PyJWT se recusa a assinar assim.
        """
        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        now = int(datetime.now(timezone.utc).timestamp())
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = b64(
            json.dumps(
                {"sub": USER_ID, "type": "access", "iat": now, "exp": now + 600}
            ).encode()
        )
        signature = b64(
            hmac.new(
                TEST_PUBLIC_KEY.encode(),
                f"{header}.{payload}".encode(),
                hashlib.sha256,
            ).digest()
        )

        with pytest.raises(TokenInvalidError):
            decode_token(f"{header}.{payload}.{signature}")

    def test_token_without_required_claims_is_rejected(self):
        now = datetime.now(timezone.utc)
        incomplete = pyjwt.encode(
            {"sub": USER_ID, "exp": now + timedelta(minutes=10), "iat": now},
            TEST_PRIVATE_KEY,
            algorithm="RS256",
        )
        with pytest.raises(TokenInvalidError):  # falta o claim "type"
            decode_token(incomplete)


class TestHashToken:
    def test_is_deterministic(self):
        assert hash_token("token-abc") == hash_token("token-abc")

    def test_different_tokens_give_different_hashes(self):
        assert hash_token("token-abc") != hash_token("token-abd")

    def test_returns_sha256_hex(self):
        digest = hash_token("token-abc")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_does_not_contain_original_token(self):
        assert "token-abc" not in hash_token("token-abc")

    def test_matches_hashlib_sha256(self):
        import hashlib

        assert hash_token("x") == hashlib.sha256(b"x").hexdigest()
