# app/integrations/wb.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import random
from io import BytesIO
from typing import Any, Dict, Mapping, Optional, Tuple, Union, List

import httpx

from app.core.redis import redis

__all__ = [
    # Ошибки
    "WBError",
    # Common / Finance
    "get_seller_info",
    "get_account_balance",
    "get_account_balance_cached",
    # Диагностика
    "ping_token",
    # NM Report (Воронка продаж)
    "get_nm_report_detail",
    "get_nm_report_detail_history",
    "get_nm_report_grouped_history",
    # Search Report (Поисковые запросы)
    "get_search_report_queries",
    "get_search_report_queries_history",
    "get_search_report_grouped_history",
    "search_report_call",
    # Reports API (низкоуровневые)
    "create_warehouse_remains_report",
    "get_warehouse_remains_status",
    "download_warehouse_remains_report",
    "create_paid_acceptance_report",
    "get_paid_acceptance_status",
    "download_paid_acceptance_report",
    "create_paid_storage_report",
    "get_paid_storage_status",
    "download_paid_storage_report",
    "get_excise_report",
    "get_retention_self_purchases",
    "get_retention_substitutions",
    "get_retention_goods_labeling",
    "get_retention_characteristics_change",
    "get_region_sales",
    "get_brand_share_brands",
    "get_brand_share_parent_subjects",
    "get_brand_share_report",
    "get_hidden_products_blocked",
    "get_hidden_products_shadowed",
    "get_goods_return_report",
    # Statistics API
    "get_supplier_sales_raw",
    "get_supplier_sales",         # <-- совместимая обёртка для main.py
    # Высокоуровневые обёртки для бота
    "get_report_stocks",
    "get_report_marking",
    "get_report_withholdings",
    "get_report_paid_acceptance",
    "get_report_paid_storage",
    "get_report_sales_by_regions",
    "get_report_brand_share",
    "get_report_hidden_goods",
    "get_report_returns_transfers",
    "get_report_sales",
]

# ---------------------------------------------------------------------------
# Constants / Config
# ---------------------------------------------------------------------------

log = logging.getLogger("wb.integrations")

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"
STATISTICS_API = "https://statistics-api.wildberries.ru"

USER_AGENT = "KuzkaSellerBot/1.0 (+wb)"
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
# Low-level HTTP helpers + rate limits
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
    if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        return data["data"]
    return data

def _normalize_order_by(ob: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ob:
        return ob
    field = ob.get("field")
    mode = ob.get("mode") or ob.get("sort")
    if field is None:
        return None
    if mode not in ("asc", "desc"):
        mode = "desc"
    return {"field": field, "mode": mode}

def _sha_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

async def _respect_limit(key: str, interval_sec: float, jitter: float = 0.2) -> None:
    """
    Простой троттлинг по ключу (на аккаунт/эндпоинт):
    если до следующего «разрешено» остаётся время — подождём.
    """
    try:
        raw = await redis.get(key)
        now = time.time()
        if raw:
            try:
                next_allowed = float(raw)
                if next_allowed > now:
                    await asyncio.sleep(next_allowed - now)
            except Exception:
                pass
        next_allowed = time.time() + interval_sec + random.uniform(0.0, jitter)
        await redis.setex(key, int(interval_sec) + 2, str(next_allowed))
    except Exception:
        # Если Redis недоступен — не ждём (лучше редкий 429, чем падение)
        pass

async def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    expect_json: bool = True,
) -> Any:
    headers = _headers(token, json_body is not None)
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json_body,
                    params=dict(query) if query else None,
                )

            status = r.status_code
            txt = r.text

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

            if 500 <= status < 600:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BASE_BACKOFF * (2 ** attempt))
                    continue
                raise WBError(f"{status} {_shorten(txt)}")

            if status >= 400:
                raise WBError(f"{status} {_shorten(txt)}")

            if not expect_json:
                return r.content, dict(r.headers)

            try:
                payload = r.json()
            except json.JSONDecodeError as e:
                raise WBError(f"Некорректный JSON от WB: {e}; payload: {_shorten(txt, 500)}")

            return _unwrap_envelope(payload)

        except httpx.RequestError as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BASE_BACKOFF * (2 ** attempt))
                continue
            raise WBError(f"Сетевая ошибка WB: {e}") from e

    if last_exc:
        raise WBError(str(last_exc))
    raise WBError("Неизвестная ошибка WB")

async def _get(url: str, token: str, params: Optional[Mapping[str, Any]] = None) -> Any:
    return await _request("GET", url, token, query=params)

async def _post(url: str, token: str, payload: Mapping[str, Any]) -> Any:
    return await _request("POST", url, token, json_body=payload)

async def _get_bytes(url: str, token: str, params: Optional[Mapping[str, Any]] = None) -> Tuple[bytes, Dict[str, str]]:
    return await _request("GET", url, token, query=params, expect_json=False)

# ---------------------------------------------------------------------------
# Common API
# ---------------------------------------------------------------------------

async def get_seller_info(token: str) -> Dict[str, Any]:
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
    url = f"{FINANCE_API}/api/v1/account/balance"
    raw = await _get(url, token)
    try:
        log.info("WB Finance raw payload: %s", _shorten(json.dumps(raw, ensure_ascii=False)))
    except Exception:
        log.info("WB Finance raw payload (non-json-serializable)")
    norm = _normalize_balance_payload(raw if isinstance(raw, Mapping) else {})
    return norm

async def get_account_balance_cached(token: str, ttl: int = 60) -> Dict[str, Any]:
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
    async def _wrap(name: str, fut):
        t0 = time.perf_counter()
        try:
            await fut
            return name, {"ok": True, "ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:
            return name, {"ok": False, "ms": int((time.perf_counter() - t0) * 1000), "error": str(e)}

    tasks = [
        _wrap("seller-info", get_seller_info(token)),
        _wrap("account-balance", get_account_balance(token)),
    ]
    res = await asyncio.gather(*tasks)
    return {k: v for k, v in res}

# ---------------------------------------------------------------------------
# Seller Analytics API — NM Report (Воронка продаж)
# ---------------------------------------------------------------------------

async def get_nm_report_detail(
    token: str,
    period_begin: str,
    period_end: str,
    *,
    timezone: str = "Europe/Moscow",
    page: int = 1,
    brand_names: Optional[List[str]] = None,
    object_ids: Optional[List[int]] = None,
    tag_ids: Optional[List[int]] = None,
    nm_ids: Optional[List[int]] = None,
    order_by: Optional[dict] = None,
    all_pages: bool = False,
    max_pages: int = 20,
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    url = f"{ANALYTICS_API}/api/v2/nm-report/detail"
    payload: Dict[str, Any] = {
        "brandNames": brand_names or [],
        "objectIDs": object_ids or [],
        "tagIDs": tag_ids or [],
        "nmIDs": nm_ids or [],
        "timezone": timezone,
        "period": {"begin": period_begin, "end": period_end},
        "orderBy": _normalize_order_by(order_by) or {"field": "openCard", "mode": "desc"},
        "page": page,
    }

    first = await _post(url, token, payload)

    if not all_pages:
        return first

    def extract_items(obj: Any) -> List[Dict[str, Any]]:
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("cards", "cardAnaliticsData", "analyticsData", "items", "rows", "list"):
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

async def get_nm_report_detail_history(
    token: str,
    nm_ids: List[int],
    period_begin: str,
    period_end: str,
    *,
    timezone: str = "Europe/Moscow",
    aggregation_level: str = "day",
) -> Any:
    url = f"{ANALYTICS_API}/api/v2/nm-report/detail/history"
    payload: Dict[str, Any] = {
        "nmIDs": nm_ids,
        "period": {"begin": period_begin, "end": period_end},
        "timezone": timezone,
        "aggregationLevel": aggregation_level,
    }
    return await _post(url, token, payload)

async def get_nm_report_grouped_history(
    token: str,
    *,
    period_begin: str,
    period_end: str,
    object_ids: Optional[List[int]] = None,
    brand_names: Optional[List[str]] = None,
    tag_ids: Optional[List[int]] = None,
    timezone: str = "Europe/Moscow",
    aggregation_level: str = "day",
) -> Any:
    url = f"{ANALYTICS_API}/api/v2/nm-report/grouped/history"
    payload: Dict[str, Any] = {
        "objectIDs": object_ids or [],
        "brandNames": brand_names or [],
        "tagIDs": tag_ids or [],
        "period": {"begin": period_begin, "end": period_end},
        "timezone": timezone,
        "aggregationLevel": aggregation_level,
    }
    return await _post(url, token, payload)

# ---------------------------------------------------------------------------
# Seller Analytics API — Search Report
# ---------------------------------------------------------------------------

async def search_report_call(
    token: str,
    subpath: str,
    *,
    period_begin: Optional[str] = None,
    period_end: Optional[str] = None,
    timezone: str = "Europe/Moscow",
    page: Optional[int] = None,
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[List[int]] = None,
    brand_names: Optional[List[str]] = None,
    tag_ids: Optional[List[int]] = None,
    queries: Optional[List[str]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    url = f"{ANALYTICS_API}{subpath}"
    payload: Dict[str, Any] = {}

    if object_ids is not None:
        payload["objectIDs"] = object_ids
    if brand_names is not None:
        payload["brandNames"] = brand_names
    if tag_ids is not None:
        payload["tagIDs"] = tag_ids
    if queries is not None:
        payload["queries"] = queries
        payload["keywords"] = queries
    if period_begin is not None and period_end is not None:
        payload["period"] = {"begin": period_begin, "end": period_end}
    if timezone:
        payload["timezone"] = timezone
    if page is not None:
        payload["page"] = int(page)
    if order_by:
        payload["orderBy"] = _normalize_order_by(order_by)
    if extra:
        payload.update(dict(extra))

    return await _post(url, token, payload)

async def get_search_report_queries(
    token: str,
    period_begin: str,
    period_end: str,
    *,
    timezone: str = "Europe/Moscow",
    page: int = 1,
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[List[int]] = None,
    brand_names: Optional[List[str]] = None,
    tag_ids: Optional[List[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    return await search_report_call(
        token,
        "/api/v2/search-report/queries",
        period_begin=period_begin,
        period_end=period_end,
        timezone=timezone,
        page=page,
        order_by=order_by,
        object_ids=object_ids,
        brand_names=brand_names,
        tag_ids=tag_ids,
        extra=extra,
    )

async def get_search_report_queries_history(
    token: str,
    *,
    queries: Optional[List[str]] = None,
    period_begin: str,
    period_end: str,
    timezone: str = "Europe/Moscow",
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[List[int]] = None,
    brand_names: Optional[List[str]] = None,
    tag_ids: Optional[List[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    return await search_report_call(
        token,
        "/api/v2/search-report/queries/history",
        period_begin=period_begin,
        period_end=period_end,
        timezone=timezone,
        order_by=order_by,
        object_ids=object_ids,
        brand_names=brand_names,
        tag_ids=tag_ids,
        queries=queries,
        extra=extra,
    )

async def get_search_report_grouped_history(
    token: str,
    *,
    period_begin: str,
    period_end: str,
    timezone: str = "Europe/Moscow",
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[List[int]] = None,
    brand_names: Optional[List[str]] = None,
    tag_ids: Optional[List[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    return await search_report_call(
        token,
        "/api/v2/search-report/grouped/history",
        period_begin=period_begin,
        period_end=period_end,
        timezone=timezone,
        order_by=order_by,
        object_ids=object_ids,
        brand_names=brand_names,
        tag_ids=tag_ids,
        extra=extra,
    )

# ---------------------------------------------------------------------------
# Reports API — Низкоуровневые вызовы
# ---------------------------------------------------------------------------

# === Остатки на складах (JSON create/status/download) =======================

async def create_warehouse_remains_report(
    token: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    # Лимит: 1/мин (всплеск 5) — бережно ставим минимум 60с
    await _respect_limit(f"wb:rl:wr:create:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains"
    return await _get(url, token, params=params or {})

async def get_warehouse_remains_status(token: str, task_id: Union[str, int]) -> Dict[str, Any]:
    # Лимит: 1 запрос/5 секунд
    await _respect_limit(f"wb:rl:wr:status:{_sha_token(token)}", 5)
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains/tasks/{task_id}/status"
    return await _get(url, token)

async def download_warehouse_remains_report(token: str, task_id: Union[str, int]) -> List[Dict[str, Any]]:
    # Лимит: 1/мин
    await _respect_limit(f"wb:rl:wr:download:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains/tasks/{task_id}/download"
    data = await _get(url, token)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    raise WBError("Неожиданный формат ответа download warehouse remains")

# === Платная приёмка (XLSX) =================================================

async def create_paid_acceptance_report(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    await _respect_limit(f"wb:rl:accept:create:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/acceptance_report"
    return await _get(url, token, params=params or {})

async def get_paid_acceptance_status(token: str, task_id: Union[str, int]) -> Dict[str, Any]:
    await _respect_limit(f"wb:rl:accept:status:{_sha_token(token)}", 5)
    url = f"{ANALYTICS_API}/api/v1/acceptance_report/status"
    return await _get(url, token, params={"id": task_id})

async def download_paid_acceptance_report(token: str, task_id: Union[str, int]) -> Tuple[str, bytes]:
    await _respect_limit(f"wb:rl:accept:download:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/acceptance_report/report"
    content, headers = await _get_bytes(url, token, params={"id": task_id})
    fname = (headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ') or "acceptance_report.xlsx"
    return fname, content

# === Платное хранение (XLSX) ================================================

async def create_paid_storage_report(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    await _respect_limit(f"wb:rl:storage:create:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/paid_storage"
    return await _get(url, token, params=params or {})

async def get_paid_storage_status(token: str, task_id: Union[str, int]) -> Dict[str, Any]:
    await _respect_limit(f"wb:rl:storage:status:{_sha_token(token)}", 5)
    url = f"{ANALYTICS_API}/api/v1/paid_storage/status"
    return await _get(url, token, params={"id": task_id})

async def download_paid_storage_report(token: str, task_id: Union[str, int]) -> Tuple[str, bytes]:
    await _respect_limit(f"wb:rl:storage:download:{_sha_token(token)}", 60)
    url = f"{ANALYTICS_API}/api/v1/paid_storage/report"
    content, headers = await _get_bytes(url, token, params={"id": task_id})
    fname = (headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ') or "paid_storage.xlsx"
    return fname, content

# === Товары с обязательной маркировкой ======================================

async def get_excise_report(token: str, *, payload: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/excise-report"
    return await _post(url, token, payload)

# === Удержания ==============================================================

async def get_retention_self_purchases(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/antifraud-details"
    return await _get(url, token, params=params)

async def get_retention_substitutions(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/incorrect-attachments"
    return await _get(url, token, params=params)

async def get_retention_goods_labeling(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/goods-labeling"
    return await _get(url, token, params=params)

async def get_retention_characteristics_change(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/characteristics-change"
    return await _get(url, token, params=params)

# === Продажи по регионам ====================================================

async def get_region_sales(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/region-sale"
    return await _get(url, token, params=params)

# === Доля бренда в продажах =================================================

async def get_brand_share_brands(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share/brands"
    return await _get(url, token, params=params or {})

async def get_brand_share_parent_subjects(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share/parent-subjects"
    return await _get(url, token, params=params or {})

async def get_brand_share_report(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share"
    return await _get(url, token, params=params)

# === Скрытые товары =========================================================

async def get_hidden_products_blocked(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/banned-products/blocked"
    return await _get(url, token, params=params or {})

async def get_hidden_products_shadowed(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/banned-products/shadowed"
    return await _get(url, token, params=params or {})

# === Возвраты и перемещение товаров ========================================

async def get_goods_return_report(token: str, *, params: Mapping[str, Any]) -> Any:
    url = f"{ANALYTICS_API}/api/v1/analytics/goods-return"
    return await _get(url, token, params=params)

# ---------------------------------------------------------------------------
# Statistics API — продажи/возвраты
# ---------------------------------------------------------------------------

async def get_supplier_sales_raw(token: str, *, date_from: str, flag: int = 0) -> Any:
    """
    GET https://statistics-api.wildberries.ru/api/v1/supplier/sales
    Лимит: 1 запрос / минуту на аккаунт.
    """
    sha = _sha_token(token)
    # короткий кэш, чтобы избежать повторных обращений в пределах минуты
    cache_key = f"wb:cache:stats:sales:{sha}:{flag}:{date_from}"
    try:
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    await _respect_limit(f"wb:rl:stats:sales:{sha}", 60)

    url = f"{STATISTICS_API}/api/v1/supplier/sales"
    params = {"dateFrom": date_from}
    if flag in (0, 1):
        params["flag"] = flag
    data = await _get(url, token, params=params)

    try:
        # держим 55с — меньше 60, чтобы не «залипать», но хватало для UX
        await redis.setex(cache_key, 55, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

    return data

# Совместимая лёгкая обёртка (как в вашем main.py)
async def get_supplier_sales(token: str, *, date_from: str, flag: int = 0) -> Any:
    return await get_supplier_sales_raw(token, date_from=date_from, flag=flag)

# ---------------------------------------------------------------------------
# Helpers for high-level wrappers
# ---------------------------------------------------------------------------

def _extract_task_id(obj: Mapping[str, Any]) -> Optional[str]:
    for k in ("taskId", "task_id", "uuid", "id"):
        v = obj.get(k)
        if v:
            return str(v)
    data = obj.get("data")
    if isinstance(data, Mapping):
        for k in ("taskId", "task_id", "uuid", "id"):
            v = data.get(k)
            if v:
                return str(v)
    return None

def _extract_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "rows", "list", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []

def _xlsx_to_rows(xlsx_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        import openpyxl  # type: ignore
    except Exception:
        log.warning("openpyxl не установлен — не смогу распарсить XLSX, отдам пустой список")
        return []
    try:
        wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=True, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else f"col{i+1}" for i, h in enumerate(next(rows_iter, []))]
        out: List[Dict[str, Any]] = []
        for row in rows_iter:
            rec = {}
            for i, val in enumerate(row):
                key = headers[i] if i < len(headers) else f"col{i+1}"
                rec[str(key)] = val
            out.append(rec)
        return out
    except Exception as e:
        log.error("Ошибка парсинга XLSX: %s", e)
        return []

async def _poll_json_report(
    create_call,
    status_call,
    download_call,
    *,
    create_params: Optional[Mapping[str, Any]] = None,
    poll_delay: float = 5.0,       # у warehouse_remains статус: 1 раз / 5 сек
    poll_timeout: float = 180.0,
) -> List[Dict[str, Any]]:
    created = await create_call(params=create_params or {})
    if not isinstance(created, Mapping):
        raise WBError("Неожиданный ответ создания задачи отчёта")
    task_id = _extract_task_id(created)
    if not task_id:
        raise WBError(f"Не удалось получить идентификатор задачи: {json.dumps(created, ensure_ascii=False)}")

    t0 = time.perf_counter()
    while True:
        st = await status_call(task_id)
        if isinstance(st, Mapping):
            data = st.get("data") if isinstance(st.get("data"), Mapping) else st
            status_str = str(data.get("status") or data.get("state") or "").lower()
            if status_str in ("done", "ready", "finished", "success"):
                break
        if time.perf_counter() - t0 > poll_timeout:
            raise WBError("Таймаут ожидания готовности отчёта")
        await asyncio.sleep(poll_delay)

    items = await download_call(task_id)
    if not isinstance(items, list):
        raise WBError("Ожидался JSON-массив при скачивании отчёта")
    return items

def _flatten_warehouse_remains(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items:
        nm_id = it.get("nmId") or it.get("nmID")
        vendor = it.get("vendorCode") or it.get("supplierArticle")
        size = it.get("techSize") or it.get("size")
        volume = it.get("volume")
        brand = it.get("brand")
        subject = it.get("subjectName") or it.get("subject")
        barcode = it.get("barcode")

        warehouses = it.get("warehouses") or []
        if not isinstance(warehouses, list):
            continue

        for wh in warehouses:
            if not isinstance(wh, Mapping):
                continue
            row = {
                "brand": brand,
                "subjectName": subject,
                "supplierArticle": vendor,
                "nmID": nm_id,
                "barcode": barcode,
                "size": size,
                "volume": volume,
                "warehouseName": wh.get("warehouseName") or wh.get("name"),
                "quantity": wh.get("quantity") or wh.get("qty") or 0,
                "inWayToClient": wh.get("inWayToClient") or wh.get("inwayToClient"),
                "inWayFromClient": wh.get("inWayFromClient") or wh.get("inwayFromClient"),
            }
            out.append(row)
    return out

def _normalize_sales_row(r: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(r)
    if "nmID" not in out and "nmId" in out:
        out["nmID"] = out["nmId"]
    if "size" not in out and "techSize" in out:
        out["size"] = out["techSize"]
    if "subjectName" not in out and "subject" in out:
        out["subjectName"] = out["subject"]
    return out

# ---------------------------------------------------------------------------
# HIGH-LEVEL WRAPPERS, expected by bot.py
# ---------------------------------------------------------------------------

# --- Остатки на складах (JSON)
async def get_report_stocks(token: str) -> List[Dict[str, Any]]:
    default_params = {
        "locale": "ru",
        "groupByNm": True,
        "groupBySize": True,
    }
    items = await _poll_json_report(
        lambda params=None: create_warehouse_remains_report(token, params=params or default_params),
        lambda task_id: get_warehouse_remains_status(token, task_id),
        lambda task_id: download_warehouse_remains_report(token, task_id),
        create_params=default_params,
        poll_delay=5.0,      # соблюдаем 1 запрос / 5 сек к /status
        poll_timeout=180.0,
    )
    return _flatten_warehouse_remains(items)

# --- Платная приёмка (XLSX)
async def get_report_paid_acceptance(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    rows_xlsx = await _poll_xlsx_report(
        lambda params=None: create_paid_acceptance_report(token, params={"dateFrom": date_begin, "dateTo": date_end}),
        lambda task_id: get_paid_acceptance_status(token, task_id),
        lambda task_id: download_paid_acceptance_report(token, task_id),
        create_params={"dateFrom": date_begin, "dateTo": date_end},
        poll_delay=5.0,
        poll_timeout=240.0,
    )
    return rows_xlsx

# --- Платное хранение (XLSX)
async def get_report_paid_storage(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    rows_xlsx = await _poll_xlsx_report(
        lambda params=None: create_paid_storage_report(token, params={"dateFrom": date_begin, "dateTo": date_end}),
        lambda task_id: get_paid_storage_status(token, task_id),
        lambda task_id: download_paid_storage_report(token, task_id),
        create_params={"dateFrom": date_begin, "dateTo": date_end},
        poll_delay=5.0,
        poll_timeout=240.0,
    )
    return rows_xlsx

async def _poll_xlsx_report(
    create_call,
    status_call,
    download_call,
    *,
    create_params: Optional[Mapping[str, Any]] = None,
    poll_delay: float = 5.0,
    poll_timeout: float = 240.0,
) -> List[Dict[str, Any]]:
    created = await create_call(params=create_params or {})
    if not isinstance(created, Mapping):
        raise WBError("Неожиданный ответ создания задачи отчёта")
    task_id = _extract_task_id(created)
    if not task_id:
        raise WBError(f"Не удалось получить идентификатор задачи: {json.dumps(created, ensure_ascii=False)}")

    t0 = time.perf_counter()
    while True:
        st = await status_call(task_id)
        if isinstance(st, Mapping):
            data = st.get("data") if isinstance(st.get("data"), Mapping) else st
            status_str = str(data.get("status") or data.get("state") or "").lower()
            if status_str in ("done", "ready", "finished", "success"):
                break
        if time.perf_counter() - t0 > poll_timeout:
            raise WBError("Таймаут ожидания готовности отчёта")
        await asyncio.sleep(poll_delay)

    fname, content = await download_call(task_id)
    rows = _xlsx_to_rows(content)
    return rows

# --- Маркировка
async def get_report_marking(token: str, *, date_begin: Optional[str] = None, date_end: Optional[str] = None) -> Any:
    from datetime import date, timedelta
    if not date_begin or not date_end:
        today = date.today()
        date_end = date_end or today.isoformat()
        date_begin = date_begin or (today - timedelta(days=30)).isoformat()

    payload = {
        "dateFrom": date_begin,
        "dateTo": date_end,
        "page": 1,
        "pageSize": 500,
        "orderBy": {"field": "docDate", "mode": "desc"},
    }
    data = await get_excise_report(token, payload=payload)
    return data

# --- Удержания
async def get_report_withholdings(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    params = {"dateFrom": date_begin, "dateTo": date_end}
    parts = await asyncio.gather(
        get_retention_self_purchases(token, params=params),
        get_retention_substitutions(token, params=params),
        get_retention_goods_labeling(token, params=params),
        get_retention_characteristics_change(token, params=params),
        return_exceptions=True,
    )
    out: List[Dict[str, Any]] = []
    names = ["selfPurchase", "substitution", "goodsLabeling", "characteristicsChange"]
    for name, chunk in zip(names, parts):
        if isinstance(chunk, Exception):
            log.warning("Удержания: '%s' вернул ошибку: %s", name, chunk)
            continue
        rows = _extract_list(chunk)
        for r in rows:
            rec = dict(r)
            rec.setdefault("type", name)
            if "amount" not in rec and "sum" in rec:
                rec["amount"] = rec.get("sum")
            out.append(rec)
    return out

# --- Продажи по регионам
async def get_report_sales_by_regions(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    data = await get_region_sales(token, params={"dateFrom": date_begin, "dateTo": date_end})
    return _extract_list(data)

# --- Доля бренда
async def get_report_brand_share(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    data = await get_brand_share_report(token, params={"dateFrom": date_begin, "dateTo": date_end})
    return _extract_list(data)

# --- Скрытые товары
async def get_report_hidden_goods(token: str) -> List[Dict[str, Any]]:
    parts = await asyncio.gather(
        get_hidden_products_blocked(token),
        get_hidden_products_shadowed(token),
        return_exceptions=True,
    )
    out: List[Dict[str, Any]] = []
    labels = ["blocked", "shadowed"]
    for label, chunk in zip(labels, parts):
        if isinstance(chunk, Exception):
            log.warning("Hidden goods '%s' error: %s", label, chunk)
            continue
        rows = _extract_list(chunk)
        for r in rows:
            rec = dict(r)
            rec.setdefault("status", label)
            out.append(rec)
    return out

# --- Возвраты/перемещения
async def get_report_returns_transfers(token: str, *, date_begin: str, date_end: str) -> List[Dict[str, Any]]:
    data = await get_goods_return_report(token, params={"dateFrom": date_begin, "dateTo": date_end})
    return _extract_list(data)

# --- Продажи (Statistics API)
async def get_report_sales(
    token: str,
    *,
    date_begin: str,                # RFC3339 или YYYY-MM-DD для flag=0; для flag=1 — YYYY-MM-DD
    date_end: Optional[str] = None, # если указан и flag=1 — обойдём все дни включительно
    flag: int = 0,
    max_requests: int = 50,
) -> List[Dict[str, Any]]:
    """
    Универсальная обёртка «Продажи и возвраты».

    Режимы:
      - flag=0: курсор по lastChangeDate, начиная с date_begin. Игнорируем date_end.
      - flag=1: если date_end задан — обходим диапазон дат по дням; иначе берём только date_begin.
    """
    out: List[Dict[str, Any]] = []

    if flag == 1:
        from datetime import datetime, timedelta

        def _parse_ymd(s: str) -> datetime:
            return datetime.fromisoformat(s[:10])

        start_d = _parse_ymd(date_begin)
        end_d = _parse_ymd(date_end or date_begin)
        if end_d < start_d:
            start_d, end_d = end_d, start_d

        cur = start_d
        calls = 0
        while cur <= end_d and calls < max_requests:
            day = cur.date().isoformat()
            data = await get_supplier_sales_raw(token, date_from=day, flag=1)
            rows = _extract_list(data) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for r in rows:
                out.append(_normalize_sales_row(r if isinstance(r, dict) else {}))
            cur += timedelta(days=1)
            calls += 1
        return out

    # flag=0 — курсор по lastChangeDate
    cursor = date_begin
    prev_max_lcd = None
    requests = 0

    while requests < max_requests:
        data = await get_supplier_sales_raw(token, date_from=cursor, flag=0)
        rows = data if isinstance(data, list) else _extract_list(data)
        if not rows:
            break

        for r in rows:
            out.append(_normalize_sales_row(r if isinstance(r, dict) else {}))

        try:
            max_lcd = max((str(r.get("lastChangeDate") or "") for r in rows if isinstance(r, dict)), default=None)
        except Exception:
            max_lcd = None

        if not max_lcd or max_lcd == prev_max_lcd:
            break

        cursor = max_lcd
        prev_max_lcd = max_lcd
        requests += 1

    return out
