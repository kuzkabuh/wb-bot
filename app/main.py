from fastapi import FastAPI, Request, Response, HTTPException, Depends, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse
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
from app.integrations.wb import (
    get_seller_info,
    get_account_balance,
    ping_token,
    WBError,
)

import httpx
import os
import subprocess  # <-- был отсутствующим импортом

# Prometheus
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, Counter

# Counter to count requests per endpoint
REQ_COUNTER = Counter("app_requests_total", "Total HTTP requests", ["endpoint"])

# --- App bootstrap ---
setup_logging(settings.LOG_LEVEL)
app = FastAPI(title="Kuzka Seller Bot")

# cookie-сессии (секрет берём из мастер-ключа)
# хранение sid в cookie, содержимое — серверная сессия Starlette
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.MASTER_ENCRYPTION_KEY.split("base64:")[-1],
)

templates = Jinja2Templates(directory="app/web/templates")
bot, dp = build_bot()


# --- Utils ---
def require_auth(request: Request) -> int:
    """Ensure the user is authenticated and return tg_id."""
    if "tg_id" not in request.session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return int(request.session["tg_id"])


def is_admin_user(user: User | None) -> bool:
    """Return True if user is admin by flag or role."""
    if not user:
        return False
    return bool(getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin")


# --- Health ---
@app.get("/healthz")
async def healthz_get():
    REQ_COUNTER.labels("/healthz").inc()
    return {"status": "ok"}


@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)


# --- Root (просто редирект на /dashboard или /settings) ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if "tg_id" in request.session:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/settings", status_code=302)


# --- Auth: Telegram One-Time Token ---
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


# --- Dashboard ---
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

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first() if user else None

        if not cred:
            needs_key = True
            token = None
        else:
            try:
                token = decrypt_value(cred.wb_api_key_encrypted)
            except Exception:
                error = "Не удалось расшифровать API-ключ. Сохраните его заново в настройках."
                needs_key = True
                token = None

        if token:
            # Пробуем получить профиль и баланс
            try:
                seller = await get_seller_info(token)
            except WBError as e:
                error = f"WB seller-info: {e}"
            except Exception as e:
                error = f"WB seller-info ошибка: {e!r}"

            try:
                balance = await get_account_balance(token)
            except WBError as e:
                error = f"{error + ' | ' if error else ''}WB balance: {e}"
            except Exception as e:
                error = f"{error + ' | ' if error else ''}WB balance ошибка: {e!r}"

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


# --- Settings (get/post) ---
@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, tg_id: int = Depends(require_auth)):
    REQ_COUNTER.labels("/settings").inc()
    has_key = False
    role = "user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if user:
            role = user.role
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
            "role": role,
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    wb_api_key: str = Form(""),
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    REQ_COUNTER.labels("/settings_post").inc()
    wb_api_key = wb_api_key.strip()

    if not wb_api_key:
        # determine the user's role for the template
        role = "user"
        with SessionLocal() as db:
            usr = db.query(User).filter(User.tg_id == tg_id).first()
            if usr:
                role = usr.role
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "title": "Настройки",
                "tg_id": tg_id,
                "has_key": False,
                "saved": False,
                "error": "Укажите API ключ.",
                "role": role,
            },
        )

    token_enc, salt = encrypt_value(wb_api_key)

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not user:
            raise HTTPException(status_code=400, detail="user_not_found")

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if cred:
            cred.wb_api_key_encrypted = token_enc
            cred.salt = salt
        else:
            cred = UserCredentials(
                user_id=user.id,
                wb_api_key_encrypted=token_enc,
                salt=salt,
                key_version=1,
            )
            db.add(cred)
        db.commit()

    # Determine role again for template after saving
    role_after = "user"
    with SessionLocal() as db_role:
        usr2 = db_role.query(User).filter(User.tg_id == tg_id).first()
        if usr2:
            role_after = usr2.role

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Настройки",
            "tg_id": tg_id,
            "has_key": True,
            "saved": True,
            "error": "",
            "role": role_after,
        },
    )


# --- WhoAmI / Logout ---
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


# --- Commit management UI (admin only) ---
@app.get("/commit", response_class=HTMLResponse)
async def commit_get(request: Request, tg_id: int = Depends(require_auth)) -> HTMLResponse:
    """Form for creating a new release commit (admin only)."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not is_admin_user(user):
            raise HTTPException(status_code=403, detail="forbidden")
        role = user.role

    return templates.TemplateResponse(
        "commit.html",
        {
            "request": request,
            "title": "Создать релиз",
            "tg_id": tg_id,
            "role": role,
            "submitted": False,
            "error": "",
            "output": "",
        },
    )


@app.post("/commit", response_class=HTMLResponse)
async def commit_post(
    request: Request,
    message: str = Form(...),
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    """Run release script with provided commit message (admin only)."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not is_admin_user(user):
            raise HTTPException(status_code=403, detail="forbidden")

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = os.environ.copy()
    env["RELEASE_COMMIT_MESSAGE"] = message

    try:
        result = subprocess.run(
            ["bash", "scripts/auto_release.sh"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        output = (result.stdout or "").strip().splitlines()
        tail = "\n".join(output[-20:])
        return templates.TemplateResponse(
            "commit.html",
            {
                "request": request,
                "title": "Создать релиз",
                "tg_id": tg_id,
                "role": user.role,
                "submitted": True,
                "error": "",
                "output": tail,
            },
        )
    except subprocess.CalledProcessError as e:
        err = (e.stdout or "") + "\n" + (e.stderr or "")
        return templates.TemplateResponse(
            "commit.html",
            {
                "request": request,
                "title": "Создать релиз",
                "tg_id": tg_id,
                "role": user.role,
                "submitted": True,
                "error": err,
                "output": "",
            },
        )
    except Exception as e:
        return templates.TemplateResponse(
            "commit.html",
            {
                "request": request,
                "title": "Создать релиз",
                "tg_id": tg_id,
                "role": user.role,
                "submitted": True,
                "error": str(e),
                "output": "",
            },
        )


# --- Telegram webhook ---
@app.post(settings.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    # Валидация и прокорм апдейта aiogram
    payload = await request.json()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return Response(status_code=200)


# --- Admin: set webhook (нормализуем URL) ---
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
        try:
            js = r.json()
        except Exception:
            # Телеграм иногда отдаёт текст ошибки — вернём как есть
            return PlainTextResponse(r.text, status_code=r.status_code)

    return JSONResponse(js, status_code=r.status_code)


# --- Prometheus metrics ---
@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


# --- Web view: check token ---
@app.get("/check_token", response_class=HTMLResponse)
async def check_token_view(
    request: Request,
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    """Check stored WB token against all endpoints (web page)."""
    REQ_COUNTER.labels("/check_token").inc()
    error = ""
    results: dict[str, str] = {}

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first() if user else None

        if not cred:
            error = "API-ключ WB не найден. Добавьте его в настройках."
        else:
            try:
                token = decrypt_value(cred.wb_api_key_encrypted)
            except Exception:
                error = "Не удалось расшифровать API-ключ. Сохраните его заново."
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
