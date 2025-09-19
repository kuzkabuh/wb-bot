from __future__ import annotations

from typing import Tuple

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from app.core.config import settings
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User

import secrets


router = Router()


def url_join(base: str, path: str) -> str:
    """Нормализованное склеивание URL без двойных слешей."""
    return base.rstrip("/") + "/" + path.lstrip("/")


async def build_login_url(tg_id: int) -> str:
    """
    Генерирует одноразовую ссылку входа в веб-кабинет (живёт 10 минут).
    Кладём в Redis ключ вида: login:ott:<token> -> <tg_id>.
    """
    token = secrets.token_urlsafe(32)
    # redis.asyncio: setex(name, time, value)
    await redis.setex(f"login:ott:{token}", 600, str(tg_id))
    return url_join(str(settings.PUBLIC_BASE_URL), f"/login/tg?token={token}")


def ensure_user(tg_id: int) -> User:
    """
    Создаёт пользователя в БД, если его ещё нет. Возвращает объект User.
    Выполняется синхронно (короткая транзакция).
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == tg_id).first()
        if not user:
            user = User(tg_id=tg_id, role="user")
            db.add(user)
            db.commit()
            db.refresh(user)
        return user


def build_reply_menu() -> ReplyKeyboardBuilder:
    """Клавиатура разделов."""
    kb = ReplyKeyboardBuilder()
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Отчёты")
    kb.button(text="Настройки")
    kb.button(text="Профиль")
    kb.adjust(2, 3)
    return kb


@router.message(CommandStart())
async def cmd_start(m: Message):
    tg_id = m.from_user.id  # type: ignore[assignment]
    ensure_user(tg_id)

    kb = build_reply_menu()
    await m.answer(
        "Привет! Я Kuzka Seller Bot.\n"
        "Я уже запомнил твой Telegram ID и завёл аккаунт.\n\n"
        "Чтобы начать работу:\n"
        "1) Нажми «Профиль» и войди в кабинет по кнопке.\n"
        "2) В кабинете открой «Настройки» и сохрани свой WB API-ключ.\n\n"
        "Также доступны команды: /login (одноразовая ссылка), /id (показать ID), /register.",
        reply_markup=kb.as_markup(resize_keyboard=True),
    )


@router.message(Command("register"))
async def cmd_register(m: Message):
    tg_id = m.from_user.id  # type: ignore[assignment]
    ensure_user(tg_id)
    await m.answer(
        "Готово! Аккаунт привязан к твоему Telegram ID. Используй /login для входа в кабинет."
    )


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("login"))
async def cmd_login(m: Message):
    tg_id = m.from_user.id  # type: ignore[assignment]
    ensure_user(tg_id)
    url = await build_login_url(tg_id)
    ikb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=url)]]
    )
    await m.answer(
        "Одноразовая ссылка для входа (действует 10 минут):\n"
        f"{url}",
        reply_markup=ikb,
    )


@router.message(F.text == "Профиль")
async def on_profile(m: Message):
    tg_id = m.from_user.id  # type: ignore[assignment]
    user = ensure_user(tg_id)
    url = await build_login_url(tg_id)
    ikb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=url)]]
    )
    await m.answer(
        "Профиль\n"
        f"• Telegram ID: <code>{tg_id}</code>\n"
        f"• Роль: <b>{user.role}</b>\n\n"
        "Нажми кнопку ниже, чтобы войти в веб-кабинет:",
        reply_markup=ikb,
    )


# Заглушки на остальные разделы, чтобы клавиши отвечали:
@router.message(F.text == "Метрики")
async def on_metrics(m: Message):
    await m.answer("Раздел «Метрики» скоро будет доступен.")


@router.message(F.text == "Поставки")
async def on_supplies(m: Message):
    await m.answer("Раздел «Поставки» скоро будет доступен.")


@router.message(F.text == "Отчёты")
async def on_reports(m: Message):
    await m.answer("Раздел «Отчёты» скоро будет доступен.")


@router.message(F.text == "Настройки")
async def on_settings_btn(m: Message):
    tg_id = m.from_user.id  # type: ignore[assignment]
    ensure_user(tg_id)
    url = url_join(str(settings.PUBLIC_BASE_URL), "/settings")
    ikb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть настройки", url=url)]]
    )
    await m.answer(
        "Открой настройки в кабинете и добавь WB API-ключ, чтобы бот мог получать данные.",
        reply_markup=ikb,
    )


# --- ВАЖНО: обработчик ЛЮБОЙ НЕИЗВЕСТНОЙ КОМАНДЫ ---
# Должен идти ПОСЛЕ конкретных хендлеров /start, /login, /id, /register
# Заменяем проблемный Command() на фильтр по тексту со слеша
@router.message(F.text.startswith("/"))
async def any_command_help(m: Message):
    """Ловим любую неизвестную команду и выдаём подсказку «что отправить для начала работы»."""
    tg_id = m.from_user.id  # type: ignore[assignment]
    ensure_user(tg_id)

    kb = build_reply_menu()
    await m.answer(
        "Чтобы начать работу:\n"
        "• Отправь /start — появится меню.\n"
        "• Нажми «Профиль» и войди в кабинет по кнопке «Открыть кабинет».\n"
        "• В кабинете зайди в «Настройки» и сохрани свой WB API-ключ.\n\n"
        "Полезные команды:\n"
        "• /login — выдать одноразовую ссылку (10 мин)\n"
        "• /id — показать твой Telegram ID\n"
        "• /register — привязать аккаунт к Telegram ID",
        reply_markup=kb.as_markup(resize_keyboard=True),
    )


def _get_bot_token() -> str:
    """
    Возвращаем str-токен для aiogram.
    Если TELEGRAM_BOT_TOKEN — SecretStr (Pydantic), берём .get_secret_value().
    """
    token = settings.TELEGRAM_BOT_TOKEN
    # Pydantic v1/v2: SecretStr имеет get_secret_value()
    if hasattr(token, "get_secret_value"):
        return token.get_secret_value()  # type: ignore[attr-defined]
    return str(token)


def build_bot() -> Tuple[Bot, Dispatcher]:
    bot = Bot(
        token=_get_bot_token(),
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp