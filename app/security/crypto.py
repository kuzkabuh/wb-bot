# app/security/crypto.py
from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ---- Константы и параметры схемы
ENV_MASTER_KEY = "MASTER_ENCRYPTION_KEY"
INFO_V1 = b"wb-bot:record-key:v1"
DEFAULT_KEY_VERSION = 1
SUPPORTED_VERSIONS = {1}
SALT_BYTES = 16  # размер соли для HKDF (128 бит)


# ---- Внутренние утилиты

@lru_cache(maxsize=1)
def _load_master_key_bytes() -> bytes:
    """
    Читает и валидирует MASTER_ENCRYPTION_KEY из окружения.
    Допускаются форматы:
      - "base64:<urlsafe_b64_fernet_key>"
      - "<urlsafe_b64_fernet_key>" (обычно 44 символа)
    Возвращает сырые 32 байта (до кодирования в base64).
    """
    raw = os.environ.get(ENV_MASTER_KEY)
    if not raw:
        raise RuntimeError(f"{ENV_MASTER_KEY} is not set")

    if raw.startswith("base64:"):
        key_b64 = raw.split("base64:", 1)[1]
    else:
        key_b64 = raw

    try:
        mk = base64.urlsafe_b64decode(key_b64)
    except Exception as e:
        raise RuntimeError(f"{ENV_MASTER_KEY} is not valid urlsafe base64") from e

    if len(mk) != 32:
        raise RuntimeError(f"{ENV_MASTER_KEY} must decode to 32 bytes, got {len(mk)}")
    return mk


def _derive_record_key(master_key_bytes: bytes, salt_bytes: bytes, version: int) -> bytes:
    if version not in SUPPORTED_VERSIONS:
        raise RuntimeError(f"Unsupported key_version={version}")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt_bytes,
        info=INFO_V1,
    )
    return hkdf.derive(master_key_bytes)


def _fernet_from_raw32(raw32: bytes) -> Fernet:
    # Fernet ожидает urlsafe_b64-строку длиной 44 символа (кодирование 32 байт)
    return Fernet(base64.urlsafe_b64encode(raw32))


def _ensure_bytes_salt(salt: Optional[str]) -> bytes:
    """
    Если соль не передана — генерируем новую.
    Если строка соли передана — ожидаем urlsafe_b64.
    """
    if salt is None:
        return os.urandom(SALT_BYTES)
    try:
        salt_bytes = base64.urlsafe_b64decode(salt)
    except Exception as e:
        raise RuntimeError("Salt stored in DB is not valid urlsafe base64") from e

    # Не падаем, если размер не 16, но предупреждаем, если совсем неадекватно маленькая
    if len(salt_bytes) < 8:
        # Для продуктивного кода лучше строго 16 байт; здесь лишь защита от «битых» данных
        raise RuntimeError(f"Salt length too short: {len(salt_bytes)} bytes (expected >= 8, usually {SALT_BYTES})")
    return salt_bytes


def _salt_to_str(salt_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(salt_bytes).decode("ascii")


# ---- Публичные функции

def encrypt_value(plaintext: str, salt: Optional[str] = None) -> Tuple[str, str, int]:
    """
    Шифрует plaintext, возвращает (ciphertext, salt_str, key_version).
    - ciphertext: Fernet token (str)
    - salt_str: urlsafe_b64 строка
    - key_version: текущая версия схемы (int)
    """
    if plaintext is None:
        raise ValueError("encrypt_value: plaintext is None")
    if not isinstance(plaintext, str):
        raise TypeError("encrypt_value: plaintext must be str")

    mk = _load_master_key_bytes()
    salt_bytes = _ensure_bytes_salt(salt)
    record_key = _derive_record_key(mk, salt_bytes, DEFAULT_KEY_VERSION)
    f = _fernet_from_raw32(record_key)
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return token, _salt_to_str(salt_bytes), DEFAULT_KEY_VERSION


def decrypt_value(
    ciphertext: str,
    salt: Optional[str] = None,
    key_version: Optional[int] = DEFAULT_KEY_VERSION,
) -> str:
    """
    Дешифрует ciphertext.
    Алгоритм:
      1) Если передана соль — пробуем производный ключ (HKDF) согласно key_version.
      2) Fallback: пробуем расшифровать напрямую master-ключом (совместимость со старыми записями).
    Исключения:
      - RuntimeError с понятным описанием, если расшифровка невозможна.
    """
    if ciphertext is None:
        raise ValueError("decrypt_value: ciphertext is None")
    if not isinstance(ciphertext, str):
        raise TypeError("decrypt_value: ciphertext must be str")

    mk = _load_master_key_bytes()

    # 1) Попытка через деривированный ключ (если есть соль)
    if salt is not None:
        try:
            salt_bytes = _ensure_bytes_salt(salt)
            version = key_version or DEFAULT_KEY_VERSION
            record_key = _derive_record_key(mk, salt_bytes, version)
            f = _fernet_from_raw32(record_key)
            return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken:
            # переходим к fallback
            pass
        except Exception as e:
            # Любая другая ошибка на этом шаге — завернём с пояснением
            raise RuntimeError(f"Unable to decrypt with derived key (version={key_version}): {e}") from e

    # 2) Fallback: расшифровка напрямую мастер-ключом (старый способ)
    try:
        f_master = _fernet_from_raw32(mk)
        return f_master.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("Unable to decrypt value with given salt/master key") from e


__all__ = [
    "encrypt_value",
    "decrypt_value",
    "DEFAULT_KEY_VERSION",
]
