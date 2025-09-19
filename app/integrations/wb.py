import httpx

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"

# Base URL for seller analytics API (функции аналитики)
ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"


class WBError(Exception):
    """Custom exception class for Wildberries API errors."""
    pass


async def _get(url: str, token: str) -> dict:
    """Send a GET request to the given URL with the provided token.

    Args:
        url: The full URL of the endpoint to query.
        token: The API token without the ``Bearer `` prefix.

    Returns:
        The parsed JSON response as a dictionary.

    Raises:
        WBError: If the status code indicates an error or if rate limits are hit.
    """
    headers = {
        # Для WB обычно без "Bearer"
        "Authorization": token,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers)
    if r.status_code == 401:
        raise WBError("401 Unauthorized (проверьте API-ключ)")
    if r.status_code == 429:
        raise WBError("429 Too Many Requests (лимит WB, попробуйте позже)")
    if r.status_code >= 400:
        raise WBError(f"{r.status_code} {r.text}")
    return r.json()


async def get_seller_info(token: str) -> dict:
    """Return seller info from the common API.

    Args:
        token: Wildberries API token without the Bearer prefix.

    Returns:
        A dict representing seller information.
    """
    return await _get(f"{COMMON_API}/api/v1/seller-info", token)


async def get_account_balance(token: str) -> dict:
    """Return account balance information from the finance API.

    Args:
        token: Wildberries API token without the Bearer prefix.

    Returns:
        A dict representing account balance.
    """
    return await _get(f"{FINANCE_API}/api/v1/account/balance", token)


async def ping_token(token: str) -> dict:
    """Ping the configured WB endpoints with the provided token.

    The function calls every available WB integration endpoint and records
    whether the call succeeded or raised an exception.  The return value
    maps human‑readable endpoint names to either the string ``"ok"`` or
    the error message returned by the underlying request.

    Args:
        token: Wildberries API token.

    Returns:
        A mapping from endpoint name to ``"ok"`` or an error string.
    """
    results: dict[str, str] = {}
    endpoints = {
        "seller-info": get_seller_info,
        "account-balance": get_account_balance,
    }
    for name, func in endpoints.items():
        try:
            # we ignore the returned data, only care whether call succeeds
            await func(token)
            results[name] = "ok"
        except Exception as e:
            # record the exception string so the caller can render it
            results[name] = str(e)
    return results


async def get_nm_report_detail(
    token: str,
    period_begin: str,
    period_end: str,
    *,
    timezone: str = "Europe/Moscow",
    page: int = 1,
    brand_names: list[str] | None = None,
    object_ids: list[int] | None = None,
    tag_ids: list[int] | None = None,
    nm_ids: list[int] | None = None,
    order_by: dict | None = None,
) -> dict:
    """Request the product cards funnel report for the given period.

    This function calls the Wildberries seller analytics endpoint
    ``/api/v2/nm-report/detail`` which returns a list of product
    statistics (open card, add to cart, orders, etc.) for the
    specified period.  If no filters are provided, all product cards
    for the seller are included.  The period must not exceed the
    last 365 days.

    Args:
        token: API key for the Analytics category (without ``Bearer``).
        period_begin: Start date in ISO format (YYYY-MM-DD).
        period_end: End date in ISO format (YYYY-MM-DD).
        timezone: IANA time zone name; defaults to ``Europe/Moscow``.
        page: Page number for pagination.
        brand_names: Optional list of brand names to filter.
        object_ids: Optional list of object IDs to filter.
        tag_ids: Optional list of tag IDs to filter.
        nm_ids: Optional list of article numbers (nmIDs) to filter.
        order_by: Optional ordering specification, e.g. ``{"field": "openCard", "sort": "desc"}``.

    Returns:
        Parsed JSON response as a Python dict.

    Raises:
        WBError: On HTTP errors or non-200 status codes.
    """
    url = f"{ANALYTICS_API}/api/v2/nm-report/detail"
    payload = {
        "brandNames": brand_names or [],
        "objectIDs": object_ids or [],
        "tagIDs": tag_ids or [],
        "nmIDs": nm_ids or [],
        "timezone": timezone,
        "period": {"begin": period_begin, "end": period_end},
        "orderBy": order_by or {"field": "openCard", "sort": "desc"},
        "page": page,
    }
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code == 401:
        raise WBError("401 Unauthorized (проверьте Analytics API-ключ)")
    if r.status_code == 429:
        raise WBError("429 Too Many Requests (лимит аналитики WB, попробуйте позже)")
    if r.status_code >= 400:
        raise WBError(f"{r.status_code} {r.text}")
    return r.json()