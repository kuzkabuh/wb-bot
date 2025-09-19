from fastapi import FastAPI, Request, Response, HTTPException, Depends, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from aiogram.types import Update

from app.core.config import settings
from app.core.logging import setup_logging
from app.bot.bot import build_bot
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User, UserCredentials
from app.security.crypto import encrypt_value, decrypt_value
from app.integrations.wb import get_seller_info, get_account_balance, ping_token, WBError
import httpx

# Prometheus
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, Counter

# Counter to count requests per endpoint
REQ_COUNTER = Counter("app_requests_total", "Total HTTP requests", ["endpoint"])

setup_logging(settings.LOG_LEVEL)
app = FastAPI(title="Kuzka Seller Bot")
# cookie-сессии (секрет берём из мастер-ключа)
app.add_middleware(SessionMiddleware, secret_key=settings.MASTER_ENCRYPTION_KEY.split("base64:")[-1])
templates = Jinja2Templates(directory="app/web/templates")
bot, dp = build_bot()


def require_auth(request: Request) -> int:
    """Dependency to ensure the user is authenticated.

    Raises:
        HTTPException: if the user is not authenticated.

    Returns:
        The Telegram ID of the authenticated user.
    """
    if "tg_id" not in request.session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return int(request.session["tg_id"])


@app.get("/healthz")
async def healthz_get():
    REQ_COUNTER.labels("/healthz").inc()
    return {"status": "ok"}


@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)


@app.get("/login/tg")
async def login_tg(request: Request, token: str):
    REQ_COUNTER.labels("/login/tg").inc()
    key = f"login:ott:{token}"

    # 1) атомарно берём и удаляем
    tg_id = await redis.getdel(key)
    if not tg_id:
        # 2) «второй шанс» на повторный клик 60 сек
        tg_id = await redis.get(f"login:ott:recent:{token}")
        if not tg_id:
            raise HTTPException(status_code=400, detail="invalid_or_expired_token")
    else:
        await redis.setex(f"login:ott:recent:{token}", 60, tg_id)

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == int(tg_id)).first()
        if not user:
            user = User(tg_id=int(tg_id), role="user")
            db.add(user)
            db.commit()

    request.session["tg_id"] = str(tg_id)
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, tg_id: int = Depends(require_auth)):
    REQ_COUNTER.labels("/dashboard").inc()
    seller = None
    balance = None
    error = ""
    needs_key = False
    role = "user"

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if user:
            role = user.role

        cred = None
        if user:
            cred = db.query(UserCredentials).filter_by(user_id=user.id).first()

        if not cred:
            needs_key = True
        else:
            try:
                # decrypt_value extracts the plaintext and salt internally
                token = decrypt_value(cred.wb_api_key_encrypted)
            except Exception:
                error = "Не удалось расшифровать API-ключ. Сохраните его заново в настройках."
                needs_key = True
                token = None

            if token:
                # При получении данных используем общие функции интеграции,
                # чтобы не дублировать логику и правильно передавать заголовки.
                try:
                    seller = await get_seller_info(token)
                except WBError as e:
                    error = f"WB seller-info: {e}"
                except Exception as e:
                    error = f"WB seller-info ошибка: {e!r}"

                try:
                    balance = await get_account_balance(token)
                except WBError as e:
                    if error:
                        error += " | "
                    error += f"WB balance: {e}"
                except Exception as e:
                    if error:
                        error += " | "
                    error += f"WB balance ошибка: {e!r}"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Кабинет",
            "tg_id": tg_id,
            "role": role,
            "seller": seller,
            "balance": balance,
            "needs_key": needs_key,
            "error": error,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, tg_id: int = Depends(require_auth)):
    REQ_COUNTER.labels("/settings").inc()
    has_key = False
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if user:
            cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
            has_key = bool(cred)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Настройки",
            "tg_id": tg_id,
            "has_key": has_key,
            "saved": False,
            "error": "",
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    wb_api_key: str = Form(""),
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    REQ_COUNTER.labels("/settings_post").inc()
    if not wb_api_key.strip():
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "title": "Настройки",
                "tg_id": tg_id,
                "has_key": False,
                "saved": False,
                "error": "Укажите API ключ.",
            },
        )

    token, salt = encrypt_value(wb_api_key.strip())

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not user:
            raise HTTPException(status_code=400, detail="user_not_found")
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if cred:
            cred.wb_api_key_encrypted = token
            cred.salt = salt
        else:
            cred = UserCredentials(
                user_id=user.id,
                wb_api_key_encrypted=token,
                salt=salt,
                key_version=1,
            )
            db.add(cred)
        db.commit()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Настройки",
            "tg_id": tg_id,
            "has_key": True,
            "saved": True,
            "error": "",
        },
    )


@app.get("/auth/whoami")
async def whoami(request: Request):
    REQ_COUNTER.labels("/auth/whoami").inc()
    if "tg_id" not in request.session:
        return {"authorized": False}
    return {"authorized": True, "tg_id": int(request.session["tg_id"])}


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


# Telegram webhook endpoint
@app.post(settings.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    payload = await request.json()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return Response(status_code=200)


# Admin: set webhook (нормализуем URL)
@app.post("/admin/set_webhook")
async def set_webhook(req: Request):
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {settings.ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    base = str(settings.PUBLIC_BASE_URL).rstrip("/")
    path = settings.WEBHOOK_PATH.lstrip("/")
    url = f"{base}/{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook",
            params={"url": url, "secret_token": settings.TELEGRAM_WEBHOOK_SECRET},
        )
        js = r.json()
    return JSONResponse(js)


# Prometheus metrics
@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.get("/check_token", response_class=HTMLResponse)
async def check_token(
    request: Request,
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    """Web view that checks the stored WB token against all endpoints.

    Requires an authenticated session.  If the user has not saved a key,
    an error message is displayed.  Otherwise it pings each configured
    endpoint and shows the result.
    """
    REQ_COUNTER.labels("/check_token").inc()
    error = ""
    results: dict[str, str] = {}
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        cred = None
        if user:
            cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            error = "API‑ключ WB не найден. Добавьте его в настройках."
        else:
            try:
                token = decrypt_value(cred.wb_api_key_encrypted)
            except Exception:
                error = "Не удалось расшифровать API‑ключ. Сохраните его заново."
                token = None
            if token:
                try:
                    results = await ping_token(token)
                except Exception as e:
                    error = f"Ошибка проверки токена: {e}"
    return templates.TemplateResponse(
        "check_token.html",
        {
            "request": request,
            "title": "Проверка токена",
            "tg_id": tg_id,
            "results": results,
            "error": error,
        },
    )