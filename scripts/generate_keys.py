"""
Gera o par de chaves RSA para JWT RS256.

Uso:
    python scripts/generate_keys.py

Saída:
    private_key.pem  — chave privada (NUNCA commite)
    public_key.pem   — chave pública

Adicione ao .env:
    JWT_PRIVATE_KEY="<conteúdo de private_key.pem com \\n literal>"
    JWT_PUBLIC_KEY="<conteúdo de public_key.pem com \\n literal>"
"""
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    Path("private_key.pem").write_bytes(private_pem)
    Path("public_key.pem").write_bytes(public_pem)

    print("Chaves geradas: private_key.pem e public_key.pem")
    print()
    print("Adicione ao .env (substitua quebras de linha por \\n literal):")
    print()

    private_str = private_pem.decode().replace("\n", "\\n")
    public_str = public_pem.decode().replace("\n", "\\n")

    print(f'JWT_PRIVATE_KEY="{private_str}"')
    print()
    print(f'JWT_PUBLIC_KEY="{public_str}"')


if __name__ == "__main__":
    main()
