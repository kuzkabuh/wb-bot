from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from app.core.config import settings
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User, UserCredentials
from app.security.crypto import decrypt_value
from app.integrations.wb import (
    get_seller_info,
    get_account_balance,
    get_nm_report_detail,
    WBError,
    ping_token,
)

import subprocess
import os
import secrets
import json
import time
from typing import Tuple


# Router instance for registering handlers
router = Router()


def url_join(base: str, path: str) -> str:
    """
    Concatenate a base URL and path ensuring a single slash between.

    Args:
        base: The base URL (e.g. `https://example.com`).
        path: A path that may start with a slash.

    Returns:
        The normalized URL with one slash separating base and path.
    """
    return base.rstrip("/") + "/" + path.lstrip("/")


async def build_login_url(tg_id: int) -> str:
    """
    Generate a one-time login URL for the given Telegram ID.
    A random token is stored in Redis for 10 minutes and embedded into the login URL.
    When the user clicks the link the token is consumed by the backend.
    """
    token = secrets.token_urlsafe(32)
    # key like login:ott:<token> -> tg_id (string)
    await redis.setex(f"login:ott:{token}", 600, str(tg_id))
    return url_join(str(settings.PUBLIC_BASE_URL), f"/login/tg?token={token}")


def build_profile_menu() -> ReplyKeyboardMarkup:
    """
    Return a reply keyboard markup for the profile submenu.

    Contains buttons:
      • Баланс — показать сохранённый баланс (из Redis)
      • Обновить баланс — запросить новый баланс у WB и сохранить
      • Проверка токена — прогнать пинги по основным эндпоинтам WB
      • Назад — вернуться в главное меню
    """
    kb = ReplyKeyboardBuilder()
    kb.button(text="Баланс")
    kb.button(text="Обновить баланс")
    kb.button(text="Проверка токена")
    kb.button(text="Назад")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)


def build_reports_menu() -> ReplyKeyboardMarkup:
    """
    Return a reply keyboard markup for the reports submenu.

    Sections:
      • Метрики
      • Поставки
      • Воронка продаж
      • Дашборд
      • Назад
    """
    kb = ReplyKeyboardBuilder()
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Воронка продаж")
    kb.button(text="Дашборд")
    kb.button(text="Назад")
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True)


# ==========================
# Admin: автоматический релиз
# ==========================
@router.message(F.text == "Сделать релиз")
async def start_release(m: Message) -> None:
    """
    Initiate a new release (admin only).

    1) Проверяем, что пользователь — админ.
    2) Запускаем scripts/auto_release.sh для подготовки релиза.
    3) Ставим флаг ожидания commit message (10 минут).
    """
    # Check the user role to ensure only administrators can create releases
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user or not (getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin"):
            await m.answer("Извините, эта команда доступна только администратору.")
            return

    # Determine repository root relative to this file (bot.py is at app/bot/bot.py)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        result = subprocess.run(
            ["bash", "scripts/auto_release.sh"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=True,
        )
    except subprocess.CalledProcessError as e:
        err = (e.stdout or "") + "\n" + (e.stderr or "")
        await m.answer(f"Ошибка при подготовке релиза:\n{err}")
        return
    except Exception as e:
        await m.answer(f"Не удалось запустить скрипт релиза: {e}")
        return

    # Set a flag so the next message from this user will be treated as commit message
    await redis.setex(f"commit:await:{m.from_user.id}", 600, "true")
    await m.answer(
        "Новый раздел changelog создан. Пожалуйста, отправьте сообщение,\n"
        "которое будет использовано как commit-message для релиза.\n"
        "Например, кратко опишите изменения и добавьте детали.\n"
        "Ожидание: 10 минут."
    )


# ==========================
# Admin: перезапуск бота (мягкий)
# ==========================
@router.message(F.text == "Перезапустить бота")
async def restart_bot(m: Message) -> None:
    """
    Restart the Telegram bot process (логически).
    Здесь просто пересобираем главное меню для пользователя.
    Фактический рестарт процесса должен делать systemd/supervisor.
    """
    await start(m)


# ==========================
# Главное меню
# ==========================
@router.message(CommandStart())
async def start(m: Message) -> None:
    """
    Handle the /start command.
    Presents the user with a reply keyboard of available sections.
    """
    kb = ReplyKeyboardBuilder()
    kb.button(text="Отчёты")
    kb.button(text="Профиль")
    kb.button(text="Настройки")

    # If the user is admin, show the release / restart buttons
    is_admin = False
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if user and (getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin"):
            is_admin = True

    if is_admin:
        kb.button(text="Сделать релиз")
        kb.button(text="Перезапустить бота")
        kb.adjust(2, 2)
    else:
        kb.adjust(2, 1)

    await m.answer(
        "Привет! Я Kuzka Seller Bot.\nВыбирай раздел:",
        reply_markup=kb.as_markup(resize_keyboard=True),
    )


# ==========================
# Отчёты (подменю + разделы)
# ==========================
@router.message(F.text == "Отчёты")
async def reports_menu(m: Message) -> None:
    """Display the reports submenu."""
    await m.answer("Раздел отчётов. Выберите подраздел:", reply_markup=build_reports_menu())


@router.message(F.text == "Метрики")
async def metrics(m: Message) -> None:
    """Placeholder metrics info (будет расширено аналитикой)."""
    await m.answer("Дайджест: сегодня 0 продаж, выручка 0 ₽ (демо).", reply_markup=build_reports_menu())


@router.message(F.text == "Поставки")
async def supplies(m: Message) -> None:
    """Placeholder supply recommendations."""
    await m.answer("Рекомендации по поставкам появятся после синхронизации (демо).", reply_markup=build_reports_menu())


@router.message(F.text == "Воронка продаж")
async def sales_funnel_report(m: Message) -> None:
    """
    Generate a sales funnel (product cards) report for the last 7 days.
    Использует WB analytics endpoint get_nm_report_detail.
    """
    from datetime import date, timedelta

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "API-ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API-ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # Determine date range: last 7 days inclusive
    today = date.today()
    start_date = today - timedelta(days=7)
    period_begin = start_date.isoformat()
    period_end = today.isoformat()
    tz = "Europe/Amsterdam"

    try:
        data = await get_nm_report_detail(
            token, period_begin, period_end, timezone=tz, page=1
        )
    except WBError as e:
        return await m.answer(f"Ошибка аналитики: {e}", reply_markup=build_reports_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт: {e}", reply_markup=build_reports_menu())

    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data", "cardAnaliticsData", "analyticsData", "cards"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    num = len(items)
    lines = [f"Воронка продаж за период {period_begin} – {period_end}"]
    lines.append(f"Получено карточек: {num}")

    for item in items[:3]:
        nm_id = item.get("nmId") or item.get("nmID") or item.get("article") or "?"
        open_card = item.get("openCard") or item.get("open_card") or "?"
        add_to_cart = item.get("addToCart") or item.get("add_to_cart") or "?"
        orders = item.get("orders") or item.get("ordersCount") or "?"
        lines.append(
            f"{nm_id}: переходы={open_card}, добавления в корзину={add_to_cart}, заказы={orders}"
        )
    if num > 3:
        lines.append("…")

    await m.answer("\n".join(lines), reply_markup=build_reports_menu())


@router.message(F.text == "Дашборд")
async def dashboard_link(m: Message) -> None:
    """
    Provide a one-time login link to the user's dashboard.
    Генерируем OTT-ссылку /login/tg?token=...; дальше бэкенд редиректит в кабинет.
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    ott_url = await build_login_url(m.from_user.id)
    await m.answer(
        f"Перейдите в кабинет по ссылке: {ott_url}",
        disable_web_page_preview=True,
        reply_markup=build_reports_menu(),
    )


# ==========================
# Настройки
# ==========================
@router.message(F.text == "Настройки")
async def settings_menu(m: Message) -> None:
    """Send a link to the settings page in the web cabinet."""
    url = url_join(str(settings.PUBLIC_BASE_URL), "/dashboard")
    await m.answer(
        f"Зайди в кабинет: {url}\n(чуть позже привяжем one-time вход)",
        disable_web_page_preview=True,
    )


# ==========================
# Профиль + Баланс + Проверка токена
# ==========================
@router.message(F.text == "Профиль")
async def profile(m: Message) -> None:
    """
    Display basic seller information and present a profile submenu.
    Баланс не тянем сразу — по кнопке «Баланс»/«Обновить баланс».
    """
    # достаём пользователя и его WB API ключ
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "API-ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API-ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # кэш от WB на 55 сек (лимиты)
    cache_info = f"wb:seller_info:{m.from_user.id}"
    try:
        raw = await redis.get(cache_info)
        seller_info = json.loads(raw) if raw else await get_seller_info(token)
        if not raw:
            await redis.setex(cache_info, 55, json.dumps(seller_info, ensure_ascii=False))
    except WBError as e:
        return await m.answer(f"Ошибка WB seller-info: {e}")
    except Exception as e:
        return await m.answer(f"Ошибка seller-info: {e}")

    name = seller_info.get("name") or seller_info.get("supplierName") or "—"
    acc_id = (
        seller_info.get("id")
        or seller_info.get("accountId")
        or seller_info.get("supplierId")
        or "—"
    )

    text = f"👤 Продавец: {name}\nID аккаунта: {acc_id}"
    await m.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=build_profile_menu(),
    )


@router.message(F.text == "Проверка токена")
async def check_token_command(m: Message) -> None:
    """
    Handle the 'Проверка токена' command.
    Пингуем основные эндпоинты WB и показываем статусы.
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохрани API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "API-ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API-ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # Пингуем эндпоинты
    try:
        results = await ping_token(token)
    except Exception as e:
        return await m.answer(f"Ошибка проверки токена: {e}", reply_markup=build_profile_menu())

    lines = ["Результаты проверки токена:"]
    for name, status in results.items():
        if status == "ok":
            lines.append(f"✅ {name}")
        else:
            lines.append(f"❌ {name}: {status}")

    await m.answer("\n".join(lines), reply_markup=build_profile_menu())


@router.message(F.text == "Баланс")
async def show_balance(m: Message) -> None:
    """
    Показать сохранённый (персистентный) баланс из Redis.
    Если баланса нет — подсказать «Обновить баланс».
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохрани API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "API-ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        try:
            # Проверим хотя бы расшифровку — чтобы подсказки были корректны
            _ = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API-ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    persist_key = f"wb:balance:persist:{m.from_user.id}"
    try:
        raw = await redis.get(persist_key)
    except Exception:
        raw = None

    if not raw:
        await m.answer(
            "Баланс ещё не сохранён. Нажмите «Обновить баланс» для получения свежих данных.",
            reply_markup=build_profile_menu(),
        )
        return

    try:
        balance_data = json.loads(raw)
    except Exception:
        await m.answer(
            "Не удалось прочитать сохранённый баланс. Попробуйте обновить его.",
            reply_markup=build_profile_menu(),
        )
        return

    bal_value = (
        balance_data.get("balance")
        or balance_data.get("currentBalance")
        or balance_data.get("total")
    )

    if isinstance(bal_value, (int, float, str)):
        text = f"💰 Баланс: {bal_value}"
    else:
        keys_preview = ", ".join(list(balance_data.keys())[:6])
        text = f"💰 Баланс: формат не распознан (ключи: {keys_preview})"

    await m.answer(text, reply_markup=build_profile_menu())


@router.message(F.text == "Обновить баланс")
async def update_balance_handler(m: Message) -> None:
    """
    Обновить баланс с WB и сохранить в Redis (без TTL).
    Ограничение частоты — не чаще 1 раза в 55 секунд.
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохрани API-ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "API-ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API-ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    last_key = f"wb:balance:last:{m.from_user.id}"
    persist_key = f"wb:balance:persist:{m.from_user.id}"

    try:
        last_raw = await redis.get(last_key)
    except Exception:
        last_raw = None

    now_ts = int(time.time())
    if last_raw:
        try:
            last_ts = int(last_raw)
            if now_ts - last_ts < 55:
                wait_sec = 55 - (now_ts - last_ts)
                await m.answer(
                    f"Баланс можно обновлять не чаще, чем раз в 55 секунд. "
                    f"Попробуйте через {wait_sec} с.",
                    reply_markup=build_profile_menu(),
                )
                return
        except Exception:
            pass

    try:
        balance_data = await get_account_balance(token)
    except WBError as e:
        return await m.answer(f"Ошибка WB balance: {e}", reply_markup=build_profile_menu())
    except Exception as e:
        return await m.answer(f"Ошибка balance: {e}", reply_markup=build_profile_menu())

    try:
        await redis.set(persist_key, json.dumps(balance_data, ensure_ascii=False))
        await redis.set(last_key, str(now_ts))
    except Exception:
        # Даже если не сохранилось — выдадим пользователю результат
        pass

    await m.answer("Баланс обновлён и сохранён.", reply_markup=build_profile_menu())


# ==========================
# Навигация: Назад
# ==========================
@router.message(F.text == "Назад")
async def go_back(m: Message) -> None:
    """Вернуться в главное меню."""
    await start(m)


# ==========================
# Fallback: echo + релиз-коммит
# ==========================
@router.message()
async def echo_all_messages(m: Message) -> None:
    """
    Echo any user message back to them.
    Также, если ожидается commit-message для релиза — обработать его.
    """
    pending_key = f"commit:await:{m.from_user.id}"
    try:
        pending = await redis.get(pending_key)
    except Exception:
        pending = None

    if pending:
        # consume flag
        await redis.delete(pending_key)
        commit_msg = m.text or m.caption or ""

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        env = os.environ.copy()
        env["RELEASE_COMMIT_MESSAGE"] = commit_msg

        try:
            result = subprocess.run(
                ["bash", "scripts/auto_release.sh"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            out = (result.stdout or "").strip()
            lines = out.splitlines()
            tail = "\n".join(lines[-25:])
            await m.answer(f"Релиз выполнен. Последние строки вывода:\n{tail}")
        except subprocess.CalledProcessError as e:
            err = (e.stdout or "") + "\n" + (e.stderr or "")
            await m.answer(f"Ошибка при выполнении релиза:\n{err}")
        except Exception as e:
            await m.answer(f"Непредвиденная ошибка релиза: {e}")
        return

    # обычный echo
    content = m.text or m.caption or "(без текста)"
    await m.answer(content)
    # и вернуть главное меню
    await start(m)


def build_bot() -> Tuple[Bot, Dispatcher]:
    """Construct and return a Bot and Dispatcher instance."""
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
