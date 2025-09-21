from __future__ import annotations

import os
import time
import json
import secrets
import hashlib
import asyncio
from typing import Tuple, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from app.core.config import settings
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User, UserCredentials
from app.security.crypto import decrypt_value
from app.integrations.wb import (
    get_seller_info,
    get_account_balance_cached,   # ✅ берём кэшируемый нормализованный баланс
    get_nm_report_detail,
    WBError,
    ping_token,
)

# -------------------------------------------------
# Router
# -------------------------------------------------
router = Router()


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def url_join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


async def build_login_url(tg_id: int) -> str:
    """
    Генерируем одноразовую ссылку входа в веб: /login/tg?token=...
    Токен живёт 10 минут.
    """
    token = secrets.token_urlsafe(32)
    await redis.setex(f"login:ott:{token}", 600, str(tg_id))
    return url_join(str(settings.PUBLIC_BASE_URL), f"/login/tg?token={token}")


def build_profile_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Баланс")
    kb.button(text="Обновить баланс")
    kb.button(text="Проверка токена")
    kb.button(text="Назад")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)


def build_reports_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Воронка продаж")
    kb.button(text="Дашборд")
    kb.button(text="Назад")
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True)


def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    # разрядные пробелы + 2 знака после запятой
    s = f"{x:,.2f}".replace(",", " ")
    return s


def _pick_balance_fields(bal: dict) -> tuple[Optional[float], Optional[float], str]:
    """
    Забираем значения из нормализованного/сыро-смешанного ответа:
    поддерживаем и новые (current/for_withdraw), и старые алиасы.
    Возвращаем: (total/current, available/for_withdraw, currency)
    """
    # значение "всего"
    total = (
        bal.get("total")
        if bal.get("total") is not None
        else bal.get("current") or bal.get("currentBalance") or bal.get("balance")
    )
    # доступно к выводу
    available = (
        bal.get("available")
        if bal.get("available") is not None
        else bal.get("for_withdraw") or bal.get("forWithdraw") or bal.get("forWithdrawPresent")
    )
    currency = bal.get("currency") or "RUB"

    try:
        total = float(total) if total is not None else None
    except Exception:
        total = None
    try:
        available = float(available) if available is not None else None
    except Exception:
        available = None

    return total, available, str(currency)


def _seller_cache_key(token: str) -> str:
    return f"wb:seller_info:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


# -------------------------------------------------
# Admin: автоматический релиз
# -------------------------------------------------
@router.message(F.text == "Сделать релиз")
async def start_release(m: Message) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user or not (getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin"):
            await m.answer("Извините, эта команда доступна только администратору.")
            return

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    try:
        # неблокирующий запуск
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "scripts/auto_release.sh",
            cwd=repo_root,
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stdout.decode() if stdout else "") + "\n" + (stderr.decode() if stderr else "")
            await m.answer(f"Ошибка при подготовке релиза:\n{err}")
            return
    except Exception as e:
        await m.answer(f"Не удалось запустить скрипт релиза: {e}")
        return

    await redis.setex(f"commit:await:{m.from_user.id}", 600, "true")
    await m.answer(
        "Новый раздел changelog создан. Отправьте сообщение, "
        "которое станет commit-message. Ожидание: 10 минут."
    )


@router.message(F.text == "Перезапустить бота")
async def restart_bot(m: Message) -> None:
    await start(m)


# -------------------------------------------------
# Главное меню
# -------------------------------------------------
@router.message(CommandStart())
async def start(m: Message) -> None:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Отчёты")
    kb.button(text="Профиль")
    kb.button(text="Настройки")

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

    await m.answer("Привет! Я Kuzka Seller Bot.\nВыбирай раздел:", reply_markup=kb.as_markup(resize_keyboard=True))


# -------------------------------------------------
# Отчёты
# -------------------------------------------------
@router.message(F.text == "Отчёты")
async def reports_menu(m: Message) -> None:
    await m.answer("Раздел отчётов. Выберите подраздел:", reply_markup=build_reports_menu())


@router.message(F.text == "Метрики")
async def metrics(m: Message) -> None:
    await m.answer("Дайджест: сегодня 0 продаж, выручка 0 ₽ (демо).", reply_markup=build_reports_menu())


@router.message(F.text == "Поставки")
async def supplies(m: Message) -> None:
    await m.answer("Рекомендации по поставкам появятся после синхронизации (демо).", reply_markup=build_reports_menu())


@router.message(F.text == "Воронка продаж")
async def sales_funnel_report(m: Message) -> None:
    from datetime import date, timedelta

    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохраните API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]])
            return await m.answer("API-ключ WB не найден. Добавьте его в настройках кабинета.", reply_markup=ikb, disable_web_page_preview=True)

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]])
            return await m.answer("Не удалось расшифровать API-ключ. Сохраните его заново.", reply_markup=ikb, disable_web_page_preview=True)

    today = date.today()
    period_begin = (today - timedelta(days=7)).isoformat()
    period_end = today.isoformat()
    tz = "Europe/Moscow"

    try:
        data = await get_nm_report_detail(token, period_begin, period_end, timezone=tz, page=1)
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
    lines = [f"Воронка продаж за период {period_begin} – {period_end}", f"Получено карточек: {num}"]

    for item in items[:3]:
        nm_id = item.get("nmId") or item.get("nmID") or item.get("article") or "?"
        open_card = item.get("openCard") or item.get("open_card") or "?"
        add_to_cart = item.get("addToCart") or item.get("add_to_cart") or "?"
        orders = item.get("orders") or item.get("ordersCount") or "?"
        lines.append(f"{nm_id}: переходы={open_card}, добавления в корзину={add_to_cart}, заказы={orders}")
    if num > 3:
        lines.append("…")

    await m.answer("\n".join(lines), reply_markup=build_reports_menu())


@router.message(F.text == "Дашборд")
async def dashboard_link(m: Message) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохраните API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

    ott_url = await build_login_url(m.from_user.id)
    await m.answer(f"Перейдите в кабинет по ссылке: {ott_url}", disable_web_page_preview=True, reply_markup=build_reports_menu())


# -------------------------------------------------
# Настройки
# -------------------------------------------------
@router.message(F.text == "Настройки")
async def settings_menu(m: Message) -> None:
    login_url = await build_login_url(m.from_user.id)
    await m.answer(f"Откройте настройки в кабинете: {login_url}", disable_web_page_preview=True)


# -------------------------------------------------
# Профиль + Баланс + Проверка токена
# -------------------------------------------------
@router.message(F.text == "Профиль")
async def profile(m: Message) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохраните API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]])
            return await m.answer("API-ключ WB не найден. Добавьте его в настройках кабинета.", reply_markup=ikb, disable_web_page_preview=True)

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]])
            return await m.answer("Не удалось расшифровать API-ключ. Сохраните его заново.", reply_markup=ikb, disable_web_page_preview=True)

    # кэш seller-info по хэшу токена (60 сек)
    cache_key = _seller_cache_key(token)
    try:
        raw = await redis.get(cache_key)
        seller_info = json.loads(raw) if raw else await get_seller_info(token)
        if not raw:
            await redis.setex(cache_key, 60, json.dumps(seller_info, ensure_ascii=False))
    except WBError as e:
        return await m.answer(f"Ошибка WB seller-info: {e}")
    except Exception as e:
        return await m.answer(f"Ошибка seller-info: {e}")

    name = seller_info.get("name") or seller_info.get("supplierName") or "—"
    acc_id = (
        seller_info.get("sid")
        or seller_info.get("id")
        or seller_info.get("accountId")
        or seller_info.get("supplierId")
        or "—"
    )

    text = f"👤 Продавец: {name}\nID аккаунта: {acc_id}"
    await m.answer(text, disable_web_page_preview=True, reply_markup=build_profile_menu())


@router.message(F.text == "Проверка токена")
async def check_token_command(m: Message) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохрани API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]])
            return await m.answer("API-ключ WB не найден. Добавьте его в настройках кабинета.", reply_markup=ikb, disable_web_page_preview=True)

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]])
            return await m.answer("Не удалось расшифровать API-ключ. Сохраните его заново.", reply_markup=ikb, disable_web_page_preview=True)

    try:
        results = await ping_token(token)
    except Exception as e:
        return await m.answer(f"Ошибка проверки токена: {e}", reply_markup=build_profile_menu())

    lines = ["Результаты проверки токена:"]
    for name, val in results.items():
        if isinstance(val, dict):
            ok = bool(val.get("ok"))
            ms = val.get("ms")
            if ok:
                lines.append(f"✅ {name} ({ms} ms)")
            else:
                err = val.get("error") or "FAIL"
                lines.append(f"❌ {name}: {err} ({ms} ms)")
        else:
            # совместимость со старым форматом
            if str(val).lower().strip() == "ok":
                lines.append(f"✅ {name}")
            else:
                lines.append(f"❌ {name}: {val}")

    await m.answer("\n".join(lines), reply_markup=build_profile_menu())


@router.message(F.text == "Баланс")
async def show_balance(m: Message) -> None:
    """
    Показать сохранённый баланс из Redis (persist).
    Если нет — подсказать «Обновить баланс».
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохрани API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]])
            return await m.answer("API-ключ WB не найден. Добавьте его в настройках кабинета.", reply_markup=ikb, disable_web_page_preview=True)

        # Проверим, что ключ вообще расшифровывается (для понятной подсказки)
        try:
            _ = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]])
            return await m.answer("Не удалось расшифровать API-ключ. Сохраните его заново.", reply_markup=ikb, disable_web_page_preview=True)

    persist_key = f"wb:balance:persist:{m.from_user.id}"
    try:
        raw = await redis.get(persist_key)
    except Exception:
        raw = None

    if not raw:
        await m.answer("Баланс ещё не сохранён. Нажмите «Обновить баланс».", reply_markup=build_profile_menu())
        return

    try:
        balance_data = json.loads(raw)
    except Exception:
        await m.answer("Не удалось прочитать сохранённый баланс. Обновите его.", reply_markup=build_profile_menu())
        return

    total, available, currency = _pick_balance_fields(balance_data)
    if total is None and available is None:
        keys_preview = ", ".join(list(balance_data.keys())[:6])
        return await m.answer(f"💰 Баланс: формат не распознан (ключи: {keys_preview}).", reply_markup=build_profile_menu())

    text = f"💰 Баланс: {_fmt_money(total)} {currency}\n🔓 Доступно к выводу: {_fmt_money(available)} {currency}"
    await m.answer(text, reply_markup=build_profile_menu())


@router.message(F.text == "Обновить баланс")
async def update_balance_handler(m: Message) -> None:
    """
    Обновить баланс с WB и сохранить в Redis (persist).
    Ограничение частоты — не чаще 1 раза в 60 секунд на пользователя.
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]])
            return await m.answer("Сначала открой кабинет и сохрани API-ключ WB.", reply_markup=ikb, disable_web_page_preview=True)

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]])
            return await m.answer("API-ключ WB не найден. Добавьте его в настройках кабинета.", reply_markup=ikb, disable_web_page_preview=True)

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]])
            return await m.answer("Не удалось расшифровать API-ключ. Сохраните его заново.", reply_markup=ikb, disable_web_page_preview=True)

    last_key = f"wb:balance:last:{m.from_user.id}"
    persist_key = f"wb:balance:persist:{m.from_user.id}"

    # локальный rate-limit на 60 сек
    try:
        last_raw = await redis.get(last_key)
    except Exception:
        last_raw = None

    now_ts = int(time.time())
    if last_raw:
        try:
            last_ts = int(last_raw)
            if now_ts - last_ts < 60:
                wait_sec = 60 - (now_ts - last_ts)
                await m.answer(f"Баланс можно обновлять раз в 60 секунд. Попробуйте через {wait_sec} с.", reply_markup=build_profile_menu())
                return
        except Exception:
            pass

    # берём кэшируемый нормализованный баланс (внутри — Redis-кэш на 60с по токену)
    try:
        balance_data = await get_account_balance_cached(token)
    except WBError as e:
        return await m.answer(f"Ошибка WB balance: {e}", reply_markup=build_profile_menu())
    except Exception as e:
        return await m.answer(f"Ошибка balance: {e}", reply_markup=build_profile_menu())

    try:
        await redis.set(persist_key, json.dumps(balance_data, ensure_ascii=False))
        await redis.set(last_key, str(now_ts))
    except Exception:
        pass

    total, available, currency = _pick_balance_fields(balance_data)
    text = f"Баланс обновлён.\n💰 {_fmt_money(total)} {currency}\n🔓 {_fmt_money(available)} {currency}"
    await m.answer(text, reply_markup=build_profile_menu())


# -------------------------------------------------
# Навигация: Назад
# -------------------------------------------------
@router.message(F.text == "Назад")
async def go_back(m: Message) -> None:
    await start(m)


# -------------------------------------------------
# Fallback: echo + релиз-коммит
# -------------------------------------------------
@router.message()
async def echo_all_messages(m: Message) -> None:
    pending_key = f"commit:await:{m.from_user.id}"
    try:
        pending = await redis.get(pending_key)
    except Exception:
        pending = None

    if pending:
        await redis.delete(pending_key)
        commit_msg = m.text or m.caption or ""

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "scripts/auto_release.sh",
                cwd=repo_root,
                env={**os.environ, "RELEASE_COMMIT_MESSAGE": commit_msg},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                out = (stdout.decode() if stdout else "").strip().splitlines()
                tail = "\n".join(out[-25:])
                await m.answer(f"Релиз выполнен. Последние строки вывода:\n{tail}")
            else:
                err = (stdout.decode() if stdout else "") + "\n" + (stderr.decode() if stderr else "")
                await m.answer(f"Ошибка при выполнении релиза:\n{err}")
        except Exception as e:
            await m.answer(f"Непредвиденная ошибка релиза: {e}")
        return

    # обычный echo + возврат в главное меню
    content = m.text or m.caption or "(без текста)"
    await m.answer(content)
    await start(m)


# -------------------------------------------------
# Factory
# -------------------------------------------------
def build_bot() -> Tuple[Bot, Dispatcher]:
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
