from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from app.core.config import settings
from app.core.redis import redis
import secrets

router = Router()

def url_join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")

async def build_login_url(tg_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await redis.setex(f"login:ott:{token}", 600, str(tg_id))  # 10 минут
    return url_join(str(settings.PUBLIC_BASE_URL), f"/login/tg?token={token}")

@router.message(CommandStart())
async def start(m: Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Отчёты")
    kb.button(text="Настройки")
    kb.adjust(2, 2)
    await m.answer(
        "Привет! Я Kuzka Seller Bot.\nВыбирай раздел:",
        reply_markup=kb.as_markup(resize_keyboard=True)
    )

@router.message(F.text == "Метрики")
async def metrics(m: Message):
    await m.answer("Дайджест: сегодня 0 продаж, выручка 0 ₽ (демо).")

@router.message(F.text == "Поставки")
async def supply(m: Message):
    await m.answer("Рекомендации по поставкам появятся после синхронизации (демо).")

@router.message(F.text == "Отчёты")
async def reports(m: Message):
    url = url_join(str(settings.PUBLIC_BASE_URL), "/dashboard")
    await m.answer(f"Сформируй отчёт в кабинете: {url}", disable_web_page_preview=True)

@router.message(F.text == "Настройки")
async def settings_menu(m: Message):
    url = await build_login_url(m.from_user.id)
    ikb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=url)]]
    )
    # ВНИМАНИЕ: URL не вставляем в текст, чтобы Telegram не делал превью
    await m.answer(
        "Открой кабинет по кнопке ниже (one-time, 10 минут).",
        reply_markup=ikb,
        disable_web_page_preview=True
    )

def build_bot():
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp