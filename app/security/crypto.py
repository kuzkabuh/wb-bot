import base64, os
from typing import Tuple
from cryptography.fernet import Fernet
from app.core.config import settings

def _fernet() -> Fernet:
    key = settings.MASTER_ENCRYPTION_KEY
    assert key.startswith("base64:"), "MASTER_ENCRYPTION_KEY must start with base64:"
    raw = base64.b64decode(key.split("base64:")[1])
    k = base64.urlsafe_b64encode(raw)  # Fernet key
    return Fernet(k)

def encrypt_value(plaintext: str) -> Tuple[str, str]:
    salt = base64.urlsafe_b64encode(os.urandom(16)).decode()
    token = _fernet().encrypt((salt + ":" + plaintext).encode()).decode()
    return token, salt

def decrypt_value(token: str) -> str:
    data = _fernet().decrypt(token.encode()).decode()
    salt, value = data.split(":", 1)
    return value
