from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from aiogram.types import Update
from app.core.config import settings
from app.core.logging import setup_logging
from app.bot.bot import build_bot
import httpx

setup_logging(settings.LOG_LEVEL)
app = FastAPI(title="Kuzka Seller Bot")
templates = Jinja2Templates(directory="app/web/templates")
bot, dp = build_bot()

@app.get("/healthz")
async def healthz_get():
    return {"status": "ok"}

@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "title": "Dashboard (demo)"})

# --- Telegram webhook endpoint ---
@app.post(settings.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secret or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    payload = await request.json()
    update = Update.model_validate(payload)
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# --- Admin: set webhook (СКЛЕЙКА БЕЗ ДВОЙНЫХ СЛЭШЕЙ) ---
@app.post("/admin/set_webhook")
async def set_webhook(req: Request):
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {settings.ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

    # <<< ВАЖНО: нормализуем >>>
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
