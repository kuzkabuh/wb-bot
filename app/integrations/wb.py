import httpx

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"


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