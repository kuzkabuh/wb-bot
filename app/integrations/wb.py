# app/integrations/wb.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Mapping, Optional, Tuple, Union

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
    # Reports API (dev.wb.ru/openapi/reports)
    # Остатки по складам (асинхронно: create/status/download)
    "create_warehouse_remains_report",
    "get_warehouse_remains_status",
    "download_warehouse_remains_report",
    # Платная приёмка (асинхронно)
    "create_paid_acceptance_report",
    "get_paid_acceptance_status",
    "download_paid_acceptance_report",
    # Платное хранение (асинхронно)
    "create_paid_storage_report",
    "get_paid_storage_status",
    "download_paid_storage_report",
    # Товары с обязательной маркировкой
    "get_excise_report",
    # Удержания
    "get_retention_self_purchases",
    "get_retention_substitutions",
    "get_retention_goods_labeling",
    "get_retention_characteristics_change",
    # Продажи по регионам
    "get_region_sales",
    # Доля бренда в продажах
    "get_brand_share_brands",
    "get_brand_share_parent_subjects",
    "get_brand_share_report",
    # Скрытые товары
    "get_hidden_products_blocked",
    "get_hidden_products_shadowed",
    # Возвраты и перемещение товаров
    "get_goods_return_report",
]

# ---------------------------------------------------------------------------
# Constants / Config
# ---------------------------------------------------------------------------

log = logging.getLogger("wb.integrations")

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"

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
    """WB часто кладёт полезную нагрузку в {'data': {...}|[...]} — разворачиваем."""
    if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        return data["data"]
    return data


def _normalize_order_by(ob: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Унифицируем структуру сортировки:
      - WB ожидает {"field": "...", "mode": "asc|desc"}
      - Если прилетело {"field": "...", "sort": "..."} — конвертируем sort → mode
    """
    if not ob:
        return ob  # None
    field = ob.get("field")
    mode = ob.get("mode")
    sort = ob.get("sort")
    if mode is None and sort is not None:
        mode = sort
    if field is None:
        return None
    if mode not in ("asc", "desc"):
        mode = "desc"
    return {"field": field, "mode": mode}


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
    """
    Унифицированный запрос с:
      - корректными заголовками WB,
      - обработкой 401/429/5xx,
      - небольшими ретраями на сетевые ошибки,
      - разбором JSON и разворачиванием {'data': ...} (если expect_json=True)
    Возвращает:
      - JSON-объект/массив при expect_json=True,
      - (bytes, headers_dict) при expect_json=False.
    """
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
                # отдаём тело ошибки полностью (часто там JSON с подробностями)
                raise WBError(f"{status} { _shorten(txt) }")

            if not expect_json:
                return r.content, dict(r.headers)

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
    """Информация о продавце. Возвращает dict."""
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
    Приводим различные ответы WB к одному виду.
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
    """Финансовый баланс. Возвращает нормализованный dict (float’ы, JSON-friendly)."""
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
# Seller Analytics API — NM Report (Воронка продаж)
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
    Формат дат: 'YYYY-MM-DD HH:MM:SS'.
    Если all_pages=True — вернёт агрегированный список элементов.
    """
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
        return first  # уже развёрнутый 'data', если он был

    # --- агрегируем все страницы ---
    def extract_items(obj: Any) -> list:
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
    nm_ids: list[int],
    period_begin: str,   # YYYY-MM-DD
    period_end: str,     # YYYY-MM-DD
    *,
    timezone: str = "Europe/Moscow",
    aggregation_level: str = "day",  # day|week
) -> Any:
    """
    Статистика карточек товаров по дням.
    Формат дат: 'YYYY-MM-DD'.
    Возвращает 'data' (обычно list).
    """
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
    period_begin: str,   # YYYY-MM-DD
    period_end: str,     # YYYY-MM-DD
    object_ids: Optional[list[int]] = None,
    brand_names: Optional[list[str]] = None,
    tag_ids: Optional[list[int]] = None,
    timezone: str = "Europe/Moscow",
    aggregation_level: str = "day",
) -> Any:
    """
    Статистика групп карточек товаров по дням (по предметам/брендам/ярлыкам).
    Формат дат: 'YYYY-MM-DD'.
    Возвращает 'data' (обычно list).
    """
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
# Seller Analytics API — Search Report (Поисковые запросы)
# ---------------------------------------------------------------------------

async def search_report_call(
    token: str,
    subpath: str,
    *,
    period_begin: Optional[str] = None,  # detail: 'YYYY-MM-DD HH:MM:SS', history: 'YYYY-MM-DD'
    period_end: Optional[str] = None,
    timezone: str = "Europe/Moscow",
    page: Optional[int] = None,
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[list[int]] = None,
    brand_names: Optional[list[str]] = None,
    tag_ids: Optional[list[int]] = None,
    queries: Optional[list[str]] = None,  # если API потребует конкретные запросы
    extra: Optional[Mapping[str, Any]] = None,  # пространство для будущих параметров
) -> Any:
    """
    Универсальный вызов под эндпоинты Search Report.
    Примеры subpath:
      "/api/v2/search-report/queries"
      "/api/v2/search-report/queries/history"
      "/api/v2/search-report/grouped/history"
    """
    url = f"{ANALYTICS_API}{subpath}"
    payload: Dict[str, Any] = {}

    # Фильтры/группы (опционально)
    if object_ids is not None:
        payload["objectIDs"] = object_ids
    if brand_names is not None:
        payload["brandNames"] = brand_names
    if tag_ids is not None:
        payload["tagIDs"] = tag_ids

    # Список интересующих запросов (если метод поддерживает)
    if queries is not None:
        payload["queries"] = queries
        payload["keywords"] = queries

    # Временной интервал
    if period_begin is not None and period_end is not None:
        payload["period"] = {"begin": period_begin, "end": period_end}

    # Таймзона
    if timezone:
        payload["timezone"] = timezone

    # Пагинация
    if page is not None:
        payload["page"] = int(page)

    # Сортировка
    if order_by:
        payload["orderBy"] = _normalize_order_by(order_by)

    # Доп. поля
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
    object_ids: Optional[list[int]] = None,
    brand_names: Optional[list[str]] = None,
    tag_ids: Optional[list[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    """
    Сводная статистика по поисковым запросам за период.
    Формат дат, как правило, 'YYYY-MM-DD HH:MM:SS' (как у detail).
    """
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
    queries: Optional[list[str]] = None,   # можно указать конкретные запросы
    period_begin: str,                      # обычно 'YYYY-MM-DD'
    period_end: str,                        # обычно 'YYYY-MM-DD'
    timezone: str = "Europe/Moscow",
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[list[int]] = None,
    brand_names: Optional[list[str]] = None,
    tag_ids: Optional[list[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    """
    История по поисковым запросам по дням.
    Формат дат, как правило, 'YYYY-MM-DD' (как у history).
    """
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
    period_begin: str,   # обычно 'YYYY-MM-DD'
    period_end: str,     # обычно 'YYYY-MM-DD'
    timezone: str = "Europe/Moscow",
    order_by: Optional[Mapping[str, Any]] = None,
    object_ids: Optional[list[int]] = None,
    brand_names: Optional[list[str]] = None,
    tag_ids: Optional[list[int]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Any:
    """
    История по поисковым запросам по дням с группировкой по предметам/брендам/ярлыкам.
    """
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
# Reports API (dev.wildberries.ru/openapi/reports)
# Все эндпоинты на базе seller-analytics-api.wildberries.ru (по документации).
# ---------------------------------------------------------------------------

# === Остатки на складах (асинхронная выдача отчёта) ========================

async def create_warehouse_remains_report(
    token: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Создать отчёт «Остатки по складам».
    GET /api/v1/warehouse_remains
    Возвращает объект с идентификатором задачи (taskId/uuid и пр.).
    """
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains"
    return await _get(url, token, params=params or {})


async def get_warehouse_remains_status(
    token: str,
    task_id: Union[str, int],
) -> Dict[str, Any]:
    """
    Проверить статус задачи.
    GET /api/v1/warehouse_remains/status
    """
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains/status"
    return await _get(url, token, params={"id": task_id})


async def download_warehouse_remains_report(
    token: str,
    task_id: Union[str, int],
) -> Tuple[str, bytes]:
    """
    Скачать результат.
    GET /api/v1/warehouse_remains/report
    Возвращает (filename, content_bytes).
    """
    url = f"{ANALYTICS_API}/api/v1/warehouse_remains/report"
    content, headers = await _get_bytes(url, token, params={"id": task_id})
    fname = (headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ') or "warehouse_remains.xlsx"
    return fname, content


# === Платная приёмка (асинхронная выдача отчёта) ============================

async def create_paid_acceptance_report(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    Создать отчёт «Платная приёмка».
    GET /api/v1/acceptance_report
    """
    url = f"{ANALYTICS_API}/api/v1/acceptance_report"
    return await _get(url, token, params=params or {})


async def get_paid_acceptance_status(token: str, task_id: Union[str, int]) -> Dict[str, Any]:
    """
    Проверить статус задачи.
    GET /api/v1/acceptance_report/status
    """
    url = f"{ANALYTICS_API}/api/v1/acceptance_report/status"
    return await _get(url, token, params={"id": task_id})


async def download_paid_acceptance_report(token: str, task_id: Union[str, int]) -> Tuple[str, bytes]:
    """
    Скачать результат отчёта «Платная приёмка».
    GET /api/v1/acceptance_report/report
    """
    url = f"{ANALYTICS_API}/api/v1/acceptance_report/report"
    content, headers = await _get_bytes(url, token, params={"id": task_id})
    fname = (headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ') or "acceptance_report.xlsx"
    return fname, content


# === Платное хранение (асинхронная выдача отчёта) ===========================

async def create_paid_storage_report(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    Создать отчёт «Платное хранение».
    GET /api/v1/paid_storage
    """
    url = f"{ANALYTICS_API}/api/v1/paid_storage"
    return await _get(url, token, params=params or {})


async def get_paid_storage_status(token: str, task_id: Union[str, int]) -> Dict[str, Any]:
    """
    Проверить статус задачи.
    GET /api/v1/paid_storage/status
    """
    url = f"{ANALYTICS_API}/api/v1/paid_storage/status"
    return await _get(url, token, params={"id": task_id})


async def download_paid_storage_report(token: str, task_id: Union[str, int]) -> Tuple[str, bytes]:
    """
    Скачать результат «Платное хранение».
    GET /api/v1/paid_storage/report
    """
    url = f"{ANALYTICS_API}/api/v1/paid_storage/report"
    content, headers = await _get_bytes(url, token, params={"id": task_id})
    fname = (headers.get("Content-Disposition") or "").split("filename=")[-1].strip('"; ') or "paid_storage.xlsx"
    return fname, content


# === Товары с обязательной маркировкой ======================================

async def get_excise_report(token: str, *, payload: Mapping[str, Any]) -> Any:
    """
    Возвращает операции с маркированными товарами.
    POST /api/v1/analytics/excise-report
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/excise-report"
    return await _post(url, token, payload)


# === Удержания ==============================================================

async def get_retention_self_purchases(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Самовыкупы.
    GET /api/v1/analytics/retention/antifraud-details
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/antifraud-details"
    return await _get(url, token, params=params)


async def get_retention_substitutions(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Замены.
    GET /api/v1/analytics/retention/incorrect-attachments
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/incorrect-attachments"
    return await _get(url, token, params=params)


async def get_retention_goods_labeling(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Обязательная маркировка.
    GET /api/v1/analytics/retention/goods-labeling
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/goods-labeling"
    return await _get(url, token, params=params)


async def get_retention_characteristics_change(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Изменение характеристик.
    GET /api/v1/analytics/retention/characteristics-change
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/retention/characteristics-change"
    return await _get(url, token, params=params)


# === Продажи по регионам ====================================================

async def get_region_sales(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Продажи по регионам.
    GET /api/v1/analytics/region-sale
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/region-sale"
    return await _get(url, token, params=params)


# === Доля бренда в продажах =================================================

async def get_brand_share_brands(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    """
    Список брендов в категории.
    GET /api/v1/analytics/brand-share/brands
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share/brands"
    return await _get(url, token, params=params or {})


async def get_brand_share_parent_subjects(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    """
    Список родительских предметов.
    GET /api/v1/analytics/brand-share/parent-subjects
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share/parent-subjects"
    return await _get(url, token, params=params or {})


async def get_brand_share_report(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Получить отчёт «Доля бренда в продажах».
    GET /api/v1/analytics/brand-share
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/brand-share"
    return await _get(url, token, params=params)


# === Скрытые товары =========================================================

async def get_hidden_products_blocked(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    """
    Заблокированные товары.
    GET /api/v1/analytics/banned-products/blocked
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/banned-products/blocked"
    return await _get(url, token, params=params or {})


async def get_hidden_products_shadowed(token: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
    """
    Товары в теневом бане.
    GET /api/v1/analytics/banned-products/shadowed
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/banned-products/shadowed"
    return await _get(url, token, params=params or {})


# === Возвраты и перемещение товаров ========================================

async def get_goods_return_report(token: str, *, params: Mapping[str, Any]) -> Any:
    """
    Возвраты и перемещение товаров.
    GET /api/v1/analytics/goods-return
    """
    url = f"{ANALYTICS_API}/api/v1/analytics/goods-return"
    return await _get(url, token, params=params)
