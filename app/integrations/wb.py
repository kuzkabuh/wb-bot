# app/integrations/wb.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple, Union

import httpx

from app.core.redis import redis

__all__ = [
    "WBError",
    "get_seller_info",
    "get_account_balance",
    "get_account_balance_cached",
    "ping_token",
    "get_nm_report_detail",
]

# ---------------------------------------------------------------------------
# Constants / Config
# ---------------------------------------------------------------------------

log = logging.getLogger("wb.integrations")

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"

# User-Agent на будущее (можно заменить на settings.APP_NAME)
USER_AGENT = "KuzkaSellerBot/1.0 (+wb)"

# Сетевой таймаут по умолчанию
DEFAULT_TIMEOUT = 20.0

# Ретраи только на сетевые/5xx/429
MAX_RETRIES = 2
BASE_BACKOFF = 0.4  # сек


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WBError(Exception):
    """High-level error for WB API operations."""
    pass


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _shorten(text: str, n: int = 800) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."

def _headers(token: str, has_json_body: bool) -> Dict[str, str]:
    h = {
        "Authorization": token,  # у WB без Bearer
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if has_json_body:
        h["Content-Type"] = "application/json"
    return h

def _unwrap_envelope(data: Any) -> Any:
    """WB часто кладёт полезную нагрузку в {'data': {...}} — разворачиваем."""
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data

async def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """
    Унифицированный запрос с:
      - корректными заголовками WB,
      - обработкой 401/429/5xx,
      - небольшими ретраями на сетевые ошибки,
      - разбором JSON и разворачиванием {'data': {...}}.
    Возвращает уже разобранный JSON (dict/list/...), НЕ всегда dict.
    """
    headers = _headers(token, json_body is not None)
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.request(method.upper(), url, headers=headers, json=json_body)

            status = r.status_code
            txt = r.text

            # 401/429 — даём внятные ошибки (на 429 попробуем подождать/повторить)
            if status == 401:
                raise WBError("401 Unauthorized (проверьте API-ключ и права)")
            if status == 429:
                retry_after = 0.0
                try:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        retry_after = float(ra)
                except Exception:
                    retry_after = 0.0
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(2.5, retry_after or (BASE_BACKOFF * (2 ** attempt))))
                    continue
                raise WBError("429 Too Many Requests (лимит WB, попробуйте позже)")

            # 5xx — ретраим чуть-чуть
            if 500 <= status < 600:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BASE_BACKOFF * (2 ** attempt))
                    continue
                raise WBError(f"{status} { _shorten(txt) }")

            if status >= 400:
                raise WBError(f"{status} { _shorten(txt) }")

            # JSON
            try:
                payload = r.json()
            except json.JSONDecodeError as e:
                raise WBError(f"Некорректный JSON от WB: {e}; payload: { _shorten(txt, 500) }")

            return _unwrap_envelope(payload)

        except httpx.RequestError as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BASE_BACKOFF * (2 ** attempt))
                continue
            raise WBError(f"Сетевая ошибка WB: {e}") from e

    # сюда не дойдём, но для mypy
    if last_exc:
        raise WBError(str(last_exc))
    raise WBError("Неизвестная ошибка WB")


async def _get(url: str, token: str) -> Any:
    return await _request("GET", url, token)


async def _post(url: str, token: str, payload: Mapping[str, Any]) -> Any:
    return await _request("POST", url, token, json_body=payload)


# ---------------------------------------------------------------------------
# Common API
# ---------------------------------------------------------------------------

async def get_seller_info(token: str) -> Dict[str, Any]:
    """
    Информация о продавце.
    Возвращает dict.
    """
    url = f"{COMMON_API}/api/v1/seller-info"
    data = await _get(url, token)
    if not isinstance(data, dict):
        raise WBError(f"Неожиданный формат seller-info: {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# Finance API — Balance (нормализация, кэш)
# ---------------------------------------------------------------------------

def _first_present(d: Mapping[str, Any], *names: str) -> Any:
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None

def _as_float(x: Any) -> float:
    try:
        return float(str(x))
    except Exception:
        raise WBError(f"Не удалось привести значение к числу: {x!r}")

def _normalize_balance_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Приводим различные ответы WB к одному виду:

      input варианты:
        {"currency":"RUB","current":49985.47,"for_withdraw":0}
        {"currency":"RUB","currentBalance":...,"forWithdraw":...}
        {"currency":"RUB","balance":...,"forWithdrawPresent":...}
        {"currency":"RUB","total":...,"available":...}

      output:
        {
          "currency": "RUB",
          "current": <float>,          # общий/текущий
          "for_withdraw": <float>,     # доступно к выводу
          "total": <float>,            # алиас (== current)
          "available": <float>         # алиас (== for_withdraw)
        }
    """
    if not isinstance(raw, Mapping):
        raise WBError(f"Формат баланса не распознан: {type(raw).__name__}")

    currency = _first_present(raw, "currency")
    current = _first_present(raw, "current", "currentBalance", "balance", "total")
    for_withdraw = _first_present(raw, "for_withdraw", "forWithdraw", "available", "forWithdrawPresent")

    if currency is None or current is None or for_withdraw is None:
        keys = ", ".join(sorted(map(str, raw.keys()))) if isinstance(raw, Mapping) else "—"
        raise WBError(
            "Формат баланса не распознан. Ожидаем ключи вида "
            "currency/current/for_withdraw. "
            f"Получены: {keys or '(пусто)'}"
        )

    out = {
        "currency": str(currency),
        "current": _as_float(current),
        "for_withdraw": _as_float(for_withdraw),
    }
    out["total"] = out["current"]
    out["available"] = out["for_withdraw"]
    return out


async def get_account_balance(token: str) -> Dict[str, Any]:
    """
    Финансовый баланс.
    Возвращает нормализованный dict (float’ы, JSON-friendly).
    """
    url = f"{FINANCE_API}/api/v1/account/balance"
    raw = await _get(url, token)
    try:
        log.info("WB Finance raw payload: %s", _shorten(json.dumps(raw, ensure_ascii=False)))
    except Exception:
        log.info("WB Finance raw payload (non-json-serializable)")
    norm = _normalize_balance_payload(raw if isinstance(raw, Mapping) else {})
    return norm


def _sha_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

async def get_account_balance_cached(token: str, ttl: int = 60) -> Dict[str, Any]:
    """
    Обёртка над get_account_balance с кэшем в Redis на `ttl` секунд.
    Ключ — sha256(token), чтобы сам токен не светить.
    """
    key = f"wb:balance:{_sha_token(token)}"
    cached = await redis.get(key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    data = await get_account_balance(token)
    try:
        await redis.setex(key, ttl, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass
    return data


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

async def ping_token(token: str) -> Dict[str, Any]:
    """
    Параллельный прозвон основных эндпоинтов.
    Возвращает:
      {
        "seller-info":     {"ok": bool, "ms": int, "error"?: str},
        "account-balance": {"ok": bool, "ms": int, "error"?: str}
      }
    """
    async def _wrap(name: str, fut):
        t0 = time.perf_counter()
        try:
            await fut
            return name, {"ok": True, "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:
            return name, {"ok": False, "ms": int((time.perf_counter() - t0) * 1000), "error": str(e)}

    tasks = [
        _wrap("seller-info", get_seller_info(token)),
        _wrap("account-balance", get_account_balance(token)),  # без кэша — «живая» проверка
    ]
    res = await asyncio.gather(*tasks)
    return {k: v for k, v in res}


# ---------------------------------------------------------------------------
# Seller Analytics API
# ---------------------------------------------------------------------------

async def get_nm_report_detail(
    token: str,
    period_begin: str,
    period_end: str,
    *,
    timezone: str = "Europe/Moscow",
    page: int = 1,
    brand_names: Optional[list[str]] = None,
    object_ids: Optional[list[int]] = None,
    tag_ids: Optional[list[int]] = None,
    nm_ids: Optional[list[int]] = None,
    order_by: Optional[dict] = None,
    all_pages: bool = False,
    max_pages: int = 20,
) -> Union[Dict[str, Any], list[Dict[str, Any]]]:
    """
    Витрина аналитики карточек товаров за период (ограничение WB ≤ 365 дней).

    Если all_pages=True — вернёт агрегированный список элементов
    (идёт по страницам до max_pages или пока данные не закончатся).
    Иначе вернёт «как есть» (обычно dict с полями/массивом внутри).
    """
    url = f"{ANALYTICS_API}/api/v2/nm-report/detail"
    payload: Dict[str, Any] = {
        "brandNames": brand_names or [],
        "objectIDs": object_ids or [],
        "tagIDs": tag_ids or [],
        "nmIDs": nm_ids or [],
        "timezone": timezone,
        "period": {"begin": period_begin, "end": period_end},
        "orderBy": order_by or {"field": "openCard", "sort": "desc"},
        "page": page,
    }

    first = await _post(url, token, payload)

    if not all_pages:
        return first  # типизированный raw

    # --- агрегируем все страницы ---
    def extract_items(obj: Any) -> list:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("data", "cardAnaliticsData", "analyticsData", "cards", "items", "rows"):
                val = obj.get(key)
                if isinstance(val, list):
                    return val
        return []

    items = extract_items(first)
    cur_page = page
    while cur_page - page + 1 < max_pages:
        cur_page += 1
        payload["page"] = cur_page
        try:
            nxt = await _post(url, token, payload)
        except WBError:
            break
        part = extract_items(nxt)
        if not part:
            break
        items.extend(part)

    return items
