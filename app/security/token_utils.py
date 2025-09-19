import re

# JWT: три base64url-сегмента, каждый ≥1 символа
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

def sanitize_wb_token(raw: str) -> str:
    """
    Убирает кавычки, пробелы/переносы, префикс Bearer.
    Проверяет, что это валидный по форме JWT (3 base64url сегмента).
    """
    if raw is None:
        raise ValueError("WB API token is empty")

    t = raw.strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    t = "".join(ch for ch in t if ch.isprintable() and not ch.isspace())

    if not JWT_RE.fullmatch(t):
        raise ValueError(f"WB API token looks malformed (not a JWT): {t[:10]}...")
    return t
