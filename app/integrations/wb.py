import httpx

COMMON_API = "https://common-api.wildberries.ru"
FINANCE_API = "https://finance-api.wildberries.ru"

class WBError(Exception):
    pass

async def _get(url: str, token: str) -> dict:
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
    return await _get(f"{COMMON_API}/api/v1/seller-info", token)

async def get_account_balance(token: str) -> dict:
    return await _get(f"{FINANCE_API}/api/v1/account/balance", token)