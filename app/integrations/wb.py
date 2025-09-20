# app/integrations/wb.py
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional

import httpx

log = logging.getLogger("wb.integrations")

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"
# Base URL for seller analytics API (функции аналитики)
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"


class WBError(Exception):
    """Custom exception class for Wildberries API errors."""
    pass


async def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Optional[Mapping[str, Any]] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """
    Unified HTTP request helper with robust error and JSON handling.
    - Authorization header WITHOUT 'Bearer' (как у WB).
    - Friendly errors for 401/429.
    - Parses JSON and supports optional {'data': {...}} envelopes.
    """
    headers = {
        "Authorization": token,   # В WB обычно без "Bearer"
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method.upper(), url, headers=headers, json=json_body)

    status = r.status_code
    text = r.text

    if status == 401:
        raise WBError("401 Unauthorized (проверьте API-ключ и права)")
    if status == 429:
        raise WBError("429 Too Many Requests (лимит WB, попробуйте позже)")
    if status >= 400:
        raise WBError(f"{status} {text}")

    # JSON parsing with graceful degradation
    try:
        data = r.json()  # type: ignore[no-redef]
    except json.JSONDecodeError as e:
        # Иногда WB может вернуть пустой ответ/HTML при инцидентах
        raise WBError(f"Некорректный JSON от WB: {e}; payload: {text[:500]}")

    # Иногда API заворачивает полезную нагрузку в {"data": {...}}
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        return data["data"]  # type: ignore[return-value]
    if isinstance(data, dict):
        return data
    raise WBError(f"Неожиданная структура ответа WB: {type(data).__name__}")


async def _get(url: str, token: str) -> Dict[str, Any]:
    return await _request("GET", url, token)


async def _post(url: str, token: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    return await _request("POST", url, token, json_body=payload)


# -------- Common API

async def get_seller_info(token: str) -> Dict[str, Any]:
    """
    Seller info from the Common API.
    """
    url = f"{COMMON_API}/api/v1/seller-info"
    return await _get(url, token)


# -------- Finance API

def _to_decimal(value: Any) -> Decimal:
    """
    Convert value to Decimal safely (supports int/float/str).
    """
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise WBError(f"Не удалось привести значение к числу: {value!r}")


def _normalize_balance_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """
    WB обычно отвечает полями: currency, current, for_withdraw.
    Но на всякий случай поддержим 'forWithdraw' и числовые строки.
    """
    # допускаем альтернативный кейс поля:
    currency = raw.get("currency")
    current = raw.get("current")
    for_withdraw = raw.get("for_withdraw", raw.get("forWithdraw"))

    if currency is None or current is None or for_withdraw is None:
        # Подсветим, какие ключи получили
        keys = ", ".join(sorted(map(str, raw.keys())))
        raise WBError(
            f"Формат баланса не распознан. Нужны ключи: currency, current, for_withdraw; "
            f"получены: {keys or '(пусто)'}"
        )

    return {
        "currency": str(currency),
        "current": _to_decimal(current),
        "for_withdraw": _to_decimal(for_withdraw),
    }


async def get_account_balance(token: str) -> Dict[str, Any]:
    """
    Account balance from the Finance API.
    Возвращает нормализованный dict: {currency:str, current:Decimal, for_withdraw:Decimal}
    """
    url = f"{FINANCE_API}/api/v1/account/balance"
    raw = await _get(url, token)
    # Лог для отладки (можно выключить в проде)
    log.info("WB Finance raw payload: %s", json.dumps(raw, ensure_ascii=False)[:1000])
    return _normalize_balance_payload(raw)


# -------- Diagnostics

async def ping_token(token: str) -> Dict[str, str]:
    """
    Прозвон основных эндпоинтов с данным токеном.
    Возвращает {endpoint: "ok" | "error text"}.
    """
    results: Dict[str, str] = {}
    endpoints = {
        "seller-info": get_seller_info,
        "account-balance": get_account_balance,
    }
    for name, func in endpoints.items():
        try:
            await func(token)  # результат нам не важен, главное — успешность
            results[name] = "ok"
        except Exception as e:
            results[name] = str(e)
    return results


# -------- Seller Analytics API

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
) -> Dict[str, Any]:
    """
    Запрос витрины аналитики карточек товаров за период.
    Ограничение WB: не более 365 дней.
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
    raw = await _post(url, token, payload)
    return raw
