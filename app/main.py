from __future__ import annotations

import asyncio
import os
import re
from typing import Optional, Tuple, List, Dict, Any

import httpx
from aiogram.types import Update
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from starlette.middleware.sessions import SessionMiddleware

from app.bot.bot import build_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User, UserCredentials
from app.integrations.wb import (
    WBError,
    get_account_balance,
    get_seller_info,
    ping_token,
    get_supplier_sales,   # ⬅️ добавлено: статистика продаж
)
from app.security.crypto import decrypt_value, encrypt_value

# -----------------------------------------------------------------------------
# Prometheus
# -----------------------------------------------------------------------------
REQ_COUNTER = Counter("app_requests_total", "Total HTTP requests", ["endpoint"])

# -----------------------------------------------------------------------------
# App bootstrap
# -----------------------------------------------------------------------------
setup_logging(settings.LOG_LEVEL)
app = FastAPI(title="Kuzka Seller Bot")

# cookie-сессии (секрет берём из мастер-ключа)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.MASTER_ENCRYPTION_KEY.split("base64:")[-1],
)

templates = Jinja2Templates(directory="app/web/templates")

# === Jinja filters: json_pretty (правильно сериализует Decimal/даты/и т.п.) ===
from markupsafe import Markup  # noqa: E402
import json as _json  # noqa: E402
import decimal as _decimal  # noqa: E402
import datetime as _dt  # noqa: E402


def json_pretty(value) -> Markup:
    def _default(o):
        if isinstance(o, _decimal.Decimal):
            return float(o)
        if isinstance(o, (_dt.datetime, _dt.date)):
            return o.isoformat()
        return str(o)

    return Markup(_json.dumps(value, ensure_ascii=False, indent=2, default=_default))


templates.env.filters["json_pretty"] = json_pretty
# -----------------------------------------------------------------------------

bot, dp = build_bot()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

# Немного более строгая проверка одноразового токена (url-safe, 16..256)
_OTT_RE = re.compile(r"^[A-Za-z0-9\-\._~=+/]{16,256}$")


def require_auth(request: Request) -> int:
    """Ensure the user is authenticated and return tg_id."""
    if "tg_id" not in request.session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        return int(request.session["tg_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Unauthorized")


def is_admin_user(user: User | None) -> bool:
    """Return True if user is admin by flag or role."""
    if not user:
        return False
    return bool(getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin")


def _get_user_and_creds(db, tg_id: int) -> Tuple[Optional[User], Optional[UserCredentials]]:
    user = db.query(User).filter(User.tg_id == tg_id).first()
    if not user:
        return None, None
    creds = db.query(UserCredentials).filter_by(user_id=user.id).first()
    return user, creds


def _safe_decrypt(enc: str) -> Optional[str]:
    try:
        return decrypt_value(enc)
    except Exception:
        # не срываем UX, показываем дружелюбную ошибку в UI
        return None


# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.get("/healthz")
async def healthz_get():
    REQ_COUNTER.labels("/healthz").inc()
    return {"status": "ok"}


@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)


# -----------------------------------------------------------------------------
# Root
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # гость → whoami (не защищено), авторизованный → dashboard
    if "tg_id" in request.session:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/auth/whoami", status_code=302)


# -----------------------------------------------------------------------------
# Auth: Telegram One-Time Token
# -----------------------------------------------------------------------------
@app.get("/login/tg")
async def login_tg(request: Request, token: str):
    REQ_COUNTER.labels("/login/tg").inc()

    if not token or not _OTT_RE.match(token):
        raise HTTPException(status_code=400, detail="invalid_or_expired_token")

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


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, tg_id: int = Depends(require_auth)):
    REQ_COUNTER.labels("/dashboard").inc()
    seller = None
    balance = None
    error_parts: list[str] = []
    needs_key = False
    role = "user"

    with SessionLocal() as db:
        user, creds = _get_user_and_creds(db, tg_id)
        if user:
            role = user.role

        token: Optional[str] = None
        if not creds:
            needs_key = True
        else:
            token = _safe_decrypt(creds.wb_api_key_encrypted)
            if not token:
                needs_key = True
                error_parts.append("Не удалось расшифровать API-ключ. Сохраните его заново в настройках.")

        if token:
            try:
                seller = await get_seller_info(token)
            except WBError as e:
                error_parts.append(f"WB seller-info: {e}")
            except Exception as e:
                error_parts.append(f"WB seller-info ошибка: {e!r}")

            try:
                balance = await get_account_balance(token)
            except WBError as e:
                error_parts.append(f"WB balance: {e}")
            except Exception as e:
                error_parts.append(f"WB balance ошибка: {e!r}")

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
            "error": " | ".join(error_parts) if error_parts else "",
        },
    )


# -----------------------------------------------------------------------------
# Settings (get/post)
# -----------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request, tg_id: int = Depends(require_auth)):
    REQ_COUNTER.labels("/settings").inc()
    has_key = False
    role = "user"
    with SessionLocal() as db:
        user, creds = _get_user_and_creds(db, tg_id)
        if user:
            role = user.role
        has_key = bool(creds)

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
    wb_api_key = (wb_api_key or "").strip()

    if not wb_api_key:
        role = "user"
        with SessionLocal() as db:
            user, _ = _get_user_and_creds(db, tg_id)
            if user:
                role = user.role
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
        user, creds = _get_user_and_creds(db, tg_id)
        if not user:
            raise HTTPException(status_code=400, detail="user_not_found")

        if creds:
            creds.wb_api_key_encrypted = token_enc
            creds.salt = salt
        else:
            creds = UserCredentials(
                user_id=user.id,
                wb_api_key_encrypted=token_enc,
                salt=salt,
                key_version=1,
            )
            db.add(creds)
        db.commit()

    role_after = "user"
    with SessionLocal() as db_role:
        usr2, _ = _get_user_and_creds(db_role, tg_id)
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


# -----------------------------------------------------------------------------
# WhoAmI / Logout
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Commit management UI (admin only)
# -----------------------------------------------------------------------------
@app.get("/commit", response_class=HTMLResponse)
async def commit_get(request: Request, tg_id: int = Depends(require_auth)) -> HTMLResponse:
    with SessionLocal() as db:
        user, _ = _get_user_and_creds(db, tg_id)
        if not is_admin_user(user):
            raise HTTPException(status_code=403, detail="forbidden")
        role = user.role if user else "user"

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
    with SessionLocal() as db:
        user, _ = _get_user_and_creds(db, tg_id)
        if not is_admin_user(user):
            raise HTTPException(status_code=403, detail="forbidden")

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = os.environ.copy()
    env["RELEASE_COMMIT_MESSAGE"] = message

    try:
        # не блокируем event loop
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "scripts/auto_release.sh",
            cwd=repo_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            lines = (stdout.decode() if stdout else "").strip().splitlines()
            tail = "\n".join(lines[-20:])
            return templates.TemplateResponse(
                "commit.html",
                {
                    "request": request,
                    "title": "Создать релиз",
                    "tg_id": tg_id,
                    "role": getattr(user, "role", "user"),
                    "submitted": True,
                    "error": "",
                    "output": tail,
                },
            )
        else:
            err = (stdout.decode() if stdout else "") + "\n" + (stderr.decode() if stderr else "")
            return templates.TemplateResponse(
                "commit.html",
                {
                    "request": request,
                    "title": "Создать релиз",
                    "tg_id": tg_id,
                    "role": getattr(user, "role", "user"),
                    "submitted": True,
                    "error": err.strip(),
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
                "role": getattr(user, "role", "user"),
                "submitted": True,
                "error": str(e),
                "output": "",
            },
        )


# -----------------------------------------------------------------------------
# Telegram webhook
# -----------------------------------------------------------------------------
@app.post(settings.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    payload = await request.json()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return Response(status_code=200)


# -----------------------------------------------------------------------------
# Admin: webhook helpers (set / delete / info)
# -----------------------------------------------------------------------------
def _require_admin(req: Request):
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {settings.ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/admin/set_webhook")
async def set_webhook(req: Request):
    _require_admin(req)

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
            return JSONResponse(js, status_code=r.status_code)
        except Exception:
            return PlainTextResponse(r.text, status_code=r.status_code)


@app.post("/admin/delete_webhook")
async def delete_webhook(req: Request):
    _require_admin(req)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": "false"},
        )
        try:
            js = r.json()
            return JSONResponse(js, status_code=r.status_code)
        except Exception:
            return PlainTextResponse(r.text, status_code=r.status_code)


@app.get("/admin/get_webhook_info")
async def get_webhook_info(req: Request):
    _require_admin(req)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getWebhookInfo"
        )
        try:
            js = r.json()
            return JSONResponse(js, status_code=r.status_code)
        except Exception:
            return PlainTextResponse(r.text, status_code=r.status_code)


# -----------------------------------------------------------------------------
# Prometheus metrics
# -----------------------------------------------------------------------------
@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


# -----------------------------------------------------------------------------
# Web view: check token
# -----------------------------------------------------------------------------
@app.get("/check_token", response_class=HTMLResponse)
async def check_token_view(
    request: Request,
    tg_id: int = Depends(require_auth),
) -> HTMLResponse:
    """Check stored WB token against all endpoints (web page)."""
    REQ_COUNTER.labels("/check_token").inc()
    error = ""
    results: dict[str, object] = {}

    with SessionLocal() as db:
        user, creds = _get_user_and_creds(db, tg_id)

        if not user or not creds:
            error = "API-ключ WB не найден. Добавьте его в настройках."
        else:
            token = _safe_decrypt(creds.wb_api_key_encrypted)
            if not token:
                error = "Не удалось расшифровать API-ключ. Сохраните его заново."
            else:
                try:
                    results = await ping_token(token)
                except WBError as e:
                    error = f"Ошибка проверки токена (WB): {e}"
                except Exception as e:
                    error = f"Ошибка проверки токена: {e!r}"

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


# -----------------------------------------------------------------------------
# Web view: Sales preview (Statistics API)
# -----------------------------------------------------------------------------
_SALES_TABLE_HEAD = """
<style>
table.sales{border-collapse:collapse;margin-top:12px;font:14px/1.35 system-ui, -apple-system, Segoe UI, Roboto, sans-serif}
table.sales th, table.sales td{border:1px solid #ddd;padding:6px 8px;vertical-align:top}
table.sales th{background:#f6f6f6;position:sticky;top:0}
code.small{font-size:12px;color:#4b5563}
.formbox{padding:12px;border:1px solid #e5e7eb;border-radius:8px;background:#fafafa}
</style>
<h1>Отчёт «Продажи»</h1>
<div class="formbox">
  <form method="get" action="/reports/sales">
    <label>date_from (RFC3339 или YYYY-MM-DD):
      <input type="text" name="date_from" value="{date_from}" style="width:260px;margin-left:6px"/>
    </label>
    &nbsp;&nbsp;
    <label>flag:
      <select name="flag">
        <option value="0" {f0}>0 (с &gt;= lastChangeDate)</option>
        <option value="1" {f1}>1 (вся дата = date_from)</option>
      </select>
    </label>
    &nbsp;&nbsp;
    <button type="submit">Показать</button>
  </form>
  <p><code class="small">Подсказка: для выгрузки всего объёма итеративно используйте поле <b>lastChangeDate</b> из последней строки.</code></p>
</div>
"""

@app.get("/reports/sales", response_class=HTMLResponse)
async def sales_preview(
    request: Request,
    tg_id: int = Depends(require_auth),
    date_from: Optional[str] = Query(None, alias="date_from"),
    flag: int = Query(0, ge=0, le=1),
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    """
    Быстрый предпросмотр Statistics API: /api/v1/supplier/sales
    Показываем первые N строк (limit), без сохранения файла.
    """
    REQ_COUNTER.labels("/reports/sales").inc()

    # sane default: сегодня по Мск
    if not date_from:
        date_from = _dt.date.today().isoformat()

    html_parts: List[str] = [
        _SALES_TABLE_HEAD.format(date_from=date_from, f0="selected" if flag == 0 else "", f1="selected" if flag == 1 else "")
    ]

    error = ""
    rows: List[Dict[str, Any]] = []

    with SessionLocal() as db:
        user, creds = _get_user_and_creds(db, tg_id)
        if not user or not creds:
            error = "API-ключ WB не найден. Добавьте его в настройках."
        else:
            token = _safe_decrypt(creds.wb_api_key_encrypted)
            if not token:
                error = "Не удалось расшифровать API-ключ. Сохраните его заново."
            else:
                try:
                    data = await get_supplier_sales(token, date_from=date_from, flag=flag)
                    if isinstance(data, list):
                        rows = data[:limit]
                    else:
                        rows = []
                except WBError as e:
                    error = f"Ошибка WB Statistics: {e}"
                except Exception as e:
                    error = f"Ошибка запроса: {e!r}"

    if error:
        html_parts.append(f'<p style="color:#b91c1c">⚠️ {error}</p>')
    else:
        html_parts.append(f"<p>Строк: всего ≈ {len(rows)} (показаны первые {min(limit, len(rows))})</p>")
        # Выведем компактную таблицу ключевых полей
        cols = [
            "date", "lastChangeDate", "warehouseName", "regionName",
            "supplierArticle", "nmId", "techSize",
            "totalPrice", "discountPercent", "forPay", "finishedPrice",
            "saleID", "gNumber", "srid",
        ]
        head = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
        body_rows = []
        for r in rows:
            tds = []
            for c in cols:
                v = r.get(c, "")
                tds.append(f"<td>{v}</td>")
            body_rows.append("<tr>" + "".join(tds) + "</tr>")
        table = f'<table class="sales"><thead>{head}</thead><tbody>{"".join(body_rows)}</tbody></table>'
        html_parts.append(table)

    html = "\n".join(html_parts)
    return HTMLResponse(content=html)
