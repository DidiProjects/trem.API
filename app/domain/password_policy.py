from typing import Optional

# NIST SP 800-63B: length is what makes a password strong; composition rules ("one
# uppercase, one digit") push people to predictable patterns like "Senha@123".
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def check_password(password: str, username: Optional[str] = None, email: Optional[str] = None) -> str:
    """Returns the password or raises ValueError with the reason, in the user's language."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"A senha deve ter no mínimo {PASSWORD_MIN_LENGTH} caracteres")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"A senha deve ter no máximo {PASSWORD_MAX_LENGTH} caracteres")
    if len(set(password)) < 4:
        raise ValueError("A senha repete caracteres demais")

    lowered = password.lower()
    for identity in (username, email.split("@", 1)[0] if email else None):
        if identity and len(identity) >= 3 and identity.lower() in lowered:
            raise ValueError("A senha não pode conter o nome de usuário ou o e-mail")
    return password
