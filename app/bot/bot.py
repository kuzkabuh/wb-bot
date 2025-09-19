from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
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
)
import subprocess
import os
import secrets
import json
import time

# Router instance for registering handlers
router = Router()


def url_join(base: str, path: str) -> str:
    """Concatenate a base URL and path ensuring a single slash between.

    Args:
        base: The base URL (e.g. ``https://example.com``).
        path: A path that may start with a slash.

    Returns:
        The normalized URL with one slash separating base and path.
    """
    return base.rstrip("/") + "/" + path.lstrip("/")


def build_profile_menu() -> 'aiogram.types.ReplyKeyboardMarkup':
    """Return a reply keyboard markup for the profile submenu.

    The submenu contains buttons related to the user's personal data.
    Users can check their Wildberries balance, verify their token and
    navigate back to the main menu.  All profile-related actions live
    behind the "Профиль" button to keep the main menu concise.

    Returns:
        ReplyKeyboardMarkup: A keyboard with 'Баланс', 'Проверка токена' and 'Назад'.
    """
    kb = ReplyKeyboardBuilder()
    kb.button(text="Баланс")
    kb.button(text="Обновить баланс")
    kb.button(text="Проверка токена")
    kb.button(text="Назад")
    # Arrange two rows: Balance/Update, Check Token and Back in second row
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)


def build_reports_menu() -> 'aiogram.types.ReplyKeyboardMarkup':
    """Return a reply keyboard markup for the reports submenu.

    The reports submenu groups together various analytical and planning
    sections such as metrics, supply recommendations, sales funnel and
    access to the dashboard.  A 'Назад' button allows the user to
    return to the main menu.

    Returns:
        ReplyKeyboardMarkup: A keyboard with report-related actions.
    """
    kb = ReplyKeyboardBuilder()
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Воронка продаж")
    kb.button(text="Дашборд")
    kb.button(text="Назад")
    # Arrange two rows of two and a final row for the back button
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True)


@router.message(F.text == "Сделать релиз")
async def start_release(m: Message) -> None:
    """Initiate a new release (admin only).

    When an admin invokes this command, the bot will run the release
    script to generate a new changelog section and a commit draft.  It
    then prompts the admin to send a commit message, which will be used
    to complete the release.  Non‑admins are informed that the
    operation is not permitted.
    """
    # Check the user role to ensure only administrators can create releases
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user or not (
            getattr(user, "is_admin", False)
            or getattr(user, "role", "") == "admin"
        ):
            await m.answer("Извините, эта команда доступна только администратору.")
            return
    # Run the release script once to update changelog and prepare commit draft
    # Determine repository root relative to this file (bot.py is at app/bot/bot.py)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
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
        err = e.stdout + "\n" + (e.stderr or "")
        await m.answer(f"Ошибка при подготовке релиза:\n{err}")
        return
    except Exception as e:
        await m.answer(f"Не удалось запустить скрипт релиза: {e}")
        return
    # Set a flag so the next message from this user will be treated as commit message
    await redis.setex(f"commit:await:{m.from_user.id}", 600, "true")
    await m.answer(
        "Новый раздел changelog создан. Пожалуйста, отправьте сообщение,\n"
        "которое будет использовано как commit‑message для релиза.\n"
        "Например, кратко опишите изменения и добавьте дополнительные детали.\n"
        "Команда будет ждать 10 минут.",
    )


# Handler to restart the bot (admin only)
@router.message(F.text == "Перезапустить бота")
async def restart_bot(m: Message) -> None:
    """Restart the Telegram bot process.

    Only administrators may invoke this command.  The bot will exit the
    process after sending a confirmation message.  It relies on an
    external process manager (systemd, supervisor, etc.) to restart it.
    """
    # Check admin
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user or not (
            getattr(user, "is_admin", False)
            or getattr(user, "role", "") == "admin"
        ):
            await m.answer("Эта команда доступна только администратору.")
            return
    await m.answer("Бот будет перезапущен.")
    # Give the message some time to be delivered
    await m.bot.session.close()
    # Exit the process; supervisor should restart it
    os._exit(0)


async def build_login_url(tg_id: int) -> str:
    """Generate a one‑time login URL for the given Telegram ID.

    A random token is stored in Redis for 10 minutes and embedded
    into the login URL.  When the user clicks the link the token is
    consumed by the backend.
    """
    token = secrets.token_urlsafe(32)
    # store the mapping for 10 minutes
    await redis.setex(f"login:ott:{token}", 600, str(tg_id))
    return url_join(str(settings.PUBLIC_BASE_URL), f"/login/tg?token={token}")


@router.message(CommandStart())
async def start(m: Message) -> None:
    """Handle the /start command.

    Presents the user with a reply keyboard of available sections.
    We include a 'Проверка токена' button so the user can verify
    their WB API key is working.
    """
    kb = ReplyKeyboardBuilder()
    # Build main menu: group analytics under "Отчёты"
    kb.button(text="Отчёты")
    kb.button(text="Профиль")
    kb.button(text="Настройки")
    # If the user is admin, show the release button
    is_admin = False
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if user and (
            getattr(user, "is_admin", False)
            or getattr(user, "role", "") == "admin"
        ):
            is_admin = True
    if is_admin:
        kb.button(text="Сделать релиз")
        kb.button(text="Перезапустить бота")
        # Layout: two rows: two buttons on first row and two on second
        kb.adjust(2, 2)
    else:
        # Main menu layout: one row of two and a final single button
        kb.adjust(2, 1)
    await m.answer(
        "Привет! Я Kuzka Seller Bot.\nВыбирай раздел:",
        reply_markup=kb.as_markup(resize_keyboard=True),
    )


@router.message(F.text == "Метрики")
async def metrics(m: Message) -> None:
    """Send placeholder metrics information."""
    await m.answer(
        "Дайджест: сегодня 0 продаж, выручка 0 ₽ (демо).",
        reply_markup=build_reports_menu(),
    )


@router.message(F.text == "Поставки")
async def supplies(m: Message) -> None:
    """Send placeholder supply recommendations."""
    await m.answer(
        "Рекомендации по поставкам появятся после синхронизации (демо).",
        reply_markup=build_reports_menu(),
    )


@router.message(F.text == "Отчёты")
async def reports(m: Message) -> None:
    """Send a link to the dashboard for report generation."""
    url = url_join(str(settings.PUBLIC_BASE_URL), "/dashboard")
    await m.answer(
        f"Сформируй отчёт в кабинете: {url}",
        disable_web_page_preview=True,
    )


@router.message(F.text == "Настройки")
async def settings_menu(m: Message) -> None:
    """Send a link to the settings page in the web cabinet."""
    url = url_join(str(settings.PUBLIC_BASE_URL), "/dashboard")
    await m.answer(
        f"Зайди в кабинет: {url}\n(чуть позже привяжем one-time вход)",
        disable_web_page_preview=True,
    )


@router.message(F.text == "Профиль")
async def profile(m: Message) -> None:
    """Display basic seller information and present a profile submenu.

    When the user selects the "Профиль" button from the main menu, we
    fetch the seller's name and account ID from Wildberries (using
    caching to respect API limits).  The response does *not* include
    the balance; instead the balance can be retrieved on demand using
    the 'Баланс' button.  We attach a profile submenu with actions
    related to the user: check their balance, verify the API token
    against Wildberries, or return to the main menu.
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
            # decrypt_value uses only the encrypted token; salt is embedded
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
        # Try to use cached seller info to reduce API calls; if absent, fetch and cache
        raw = await redis.get(cache_info)
        seller_info = json.loads(raw) if raw else await get_seller_info(token)
        if not raw:
            await redis.setex(cache_info, 55, json.dumps(seller_info, ensure_ascii=False))
    except WBError as e:
        return await m.answer(f"Ошибка WB seller-info: {e}")
    except Exception as e:
        return await m.answer(f"Ошибка seller-info: {e}")

    # Extract basic seller details
    name = seller_info.get("name") or seller_info.get("supplierName") or "—"
    acc_id = (
        seller_info.get("id")
        or seller_info.get("accountId")
        or seller_info.get("supplierId")
        or "—"
    )

    text = f"👤 Продавец: {name}\nID аккаунта: {acc_id}"
    # Present seller info along with a submenu for balance and token check
    await m.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=build_profile_menu(),
    )


@router.message(F.text == "Отчёты")
async def reports_menu(m: Message) -> None:
    """Display the reports submenu.

    Groups together metrics, supply planning, sales funnel and dashboard actions.
    """
    await m.answer(
        "Раздел отчётов. Выберите подраздел:",
        reply_markup=build_reports_menu(),
    )


@router.message(F.text == "Проверка токена")
async def check_token_command(m: Message) -> None:
    """Handle the 'Проверка токена' command.

    Attempts to call all configured Wildberries endpoints with the stored
    API key and reports whether each call succeeded or failed.  If the
    user has not set a key yet, directs them to the web cabinet.
    """
    # Получаем пользователя и токен из БД
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API‑ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "API‑ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API‑ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # Пингуем все доступные эндпоинты
    try:
        from app.integrations.wb import ping_token
        results = await ping_token(token)
    except Exception as e:
        return await m.answer(f"Ошибка проверки токена: {e}")

    # Формируем сообщение
    lines = ["Результаты проверки токена:"]
    for name, status in results.items():
        if status == "ok":
            lines.append(f"✅ {name}")
        else:
            lines.append(f"❌ {name}: {status}")
    # Send results with the profile submenu so the user can continue navigating
    await m.answer(
        "\n".join(lines),
        reply_markup=build_profile_menu(),
    )


# new handler to display only the Wildberries balance
@router.message(F.text == "Баланс")
async def show_balance(m: Message) -> None:
    """Handle the 'Баланс' command.

    Fetches and displays the user's current balance from Wildberries.  If
    the user has not stored a token, prompts them to set one.  The
    response always includes the profile submenu so the user can check
    the token again or return to the main menu.
    """
    # Retrieve user and token similar to the profile handler
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API‑ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "API‑ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API‑ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # Retrieve persistent balance from storage; do not call API here
    persist_key = f"wb:balance:persist:{m.from_user.id}"
    try:
        raw = await redis.get(persist_key)
    except Exception:
        raw = None
    if not raw:
        # No stored balance
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
    # Determine balance value field
    bal_value = (
        balance_data.get("balance")
        or balance_data.get("currentBalance")
        or balance_data.get("total")
    )
    if isinstance(bal_value, (int, float, str)):
        text = f"💰 Баланс: {bal_value}"
    else:
        text = (
            "💰 Баланс: формат не распознан (ключи: "
            + ", ".join(list(balance_data.keys())[:6])
            + ")"
        )
    await m.answer(text, reply_markup=build_profile_menu())


# Handler to update the user's stored balance (persistent) respecting rate limits
@router.message(F.text == "Обновить баланс")
async def update_balance_handler(m: Message) -> None:
    """Update and store the user's balance in persistent storage.

    Checks when the balance was last updated and ensures that updates do not
    occur more frequently than once every 55 seconds (to respect API limits).
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API‑ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "API‑ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API‑ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
    # Check last update timestamp
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
                    f"Баланс можно обновлять не чаще, чем раз в 55 секунд. Попробуйте через {wait_sec} с.",
                    reply_markup=build_profile_menu(),
                )
                return
        except Exception:
            pass
    # Fetch new balance from WB
    try:
        balance_data = await get_account_balance(token)
    except WBError as e:
        return await m.answer(
            f"Ошибка WB balance: {e}", reply_markup=build_profile_menu()
        )
    except Exception as e:
        return await m.answer(
            f"Ошибка balance: {e}", reply_markup=build_profile_menu()
        )
    # Store persistently (in redis) with no expiry
    try:
        await redis.set(persist_key, json.dumps(balance_data, ensure_ascii=False))
        await redis.set(last_key, str(now_ts))
    except Exception:
        pass
    await m.answer(
        "Баланс обновлён и сохранён.", reply_markup=build_profile_menu()
    )


# new handler to go back to the main menu from the profile submenu
@router.message(F.text == "Назад")
async def go_back(m: Message) -> None:
    """Return the user to the main menu.

    Simply calls the start handler to rebuild the main keyboard.  The
    user's original message is ignored apart from its sender.
    """
    await start(m)


# Handler for Sales Funnel report (Воронка продаж)
@router.message(F.text == "Воронка продаж")
async def sales_funnel_report(m: Message) -> None:
    """Generate a sales funnel (product cards) report for the last 7 days.

    This handler calls the Wildberries analytics API endpoint to build a
    report of product card statistics (openCard, addToCart, orders, etc.).
    The report covers the most recent 7‑day period and uses the user's
    WB API token.  If no token is stored, the user is prompted to set
    one first.  The result is summarized: we show how many product
    cards are in the response and display the first few entries with
    key metrics.  This endpoint may be rate‑limited, so we do not
    cache its response.
    """
    from datetime import date, timedelta

    # Retrieve user and token similar to other handlers
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return await m.answer(
                "Сначала открой кабинет и сохраните API‑ключ WB.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "API‑ключ WB не найден. Добавьте его в настройках кабинета.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )
        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API‑ключ", url=login_url)]]
            )
            return await m.answer(
                "Не удалось расшифровать API‑ключ. Сохраните его заново.",
                reply_markup=ikb,
                disable_web_page_preview=True,
            )

    # Determine date range: last 7 days ending today (inclusive)
    today = date.today()
    start_date = today - timedelta(days=7)
    period_begin = start_date.isoformat()
    period_end = today.isoformat()
    # Use the user's timezone if available; default to Europe/Amsterdam
    tz = "Europe/Amsterdam"

    # Call analytics API
    try:
        data = await get_nm_report_detail(
            token,
            period_begin,
            period_end,
            timezone=tz,
            page=1,
        )
    except WBError as e:
        return await m.answer(f"Ошибка аналитики: {e}")
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт: {e}")

    # Assume the response is a list of items or has a key 'data' holding the list
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Some WB endpoints wrap results under 'data' or 'cardAnaliticsData'
        for key in ["data", "cardAnaliticsData", "analyticsData", "cards"]:
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
    num = len(items)

    lines = [f"Воронка продаж за период {period_begin} – {period_end}"]
    lines.append(f"Получено карточек: {num}")
    # Show first 3 items if available
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
    # Send the report
    await m.answer(
        "\n".join(lines),
        reply_markup=build_reports_menu(),
    )


# Handler for dashboard link inside reports submenu
@router.message(F.text == "Дашборд")
async def dashboard_link(m: Message) -> None:
    """Send a link to the dashboard when selected from the reports menu."""
    url = url_join(str(settings.PUBLIC_BASE_URL), "/dashboard")
    await m.answer(
        f"Сформируй отчёт в кабинете: {url}",
        disable_web_page_preview=True,
        reply_markup=build_reports_menu(),
    )


# Fallback echo handler: reply with the same text for any unhandled message
@router.message()
async def echo_all_messages(m: Message) -> None:
    """Echo any user message back to them.

    This handler is registered last so it only triggers if no other
    command or filter matched.  It simply replies with the text
    content of the incoming message, which can be useful for
    debugging or when users send unexpected input.
    """
    # Choose the appropriate text: use text or caption if present
    # Before echoing, check if the user is expected to provide a commit message.
    pending_key = f"commit:await:{m.from_user.id}"
    try:
        pending = await redis.get(pending_key)
    except Exception:
        pending = None
    # If pending flag exists, consume this message as a commit description
    if pending:
        # Remove the pending flag
        await redis.delete(pending_key)
        commit_msg = m.text or m.caption or ""
        # Call the release script with the commit message passed via env var
        # We run the script in the root of the project (assuming this file resides in app/bot/)
        # Determine repository root relative to this file
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
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
            out = result.stdout.strip()
            # Only show last 25 lines to avoid flooding
            lines = out.splitlines()
            tail = "\n".join(lines[-25:])
            await m.answer(f"Релиз выполнен. Последние строки вывода:\n{tail}")
        except subprocess.CalledProcessError as e:
            err = e.stdout + "\n" + (e.stderr or "")
            await m.answer(f"Ошибка при выполнении релиза:\n{err}")
        except Exception as e:
            await m.answer(f"Непредвиденная ошибка релиза: {e}")
        return
    # Otherwise, simply echo the message
    content = m.text or m.caption or "(без текста)"
    await m.answer(content)


def build_bot() -> tuple[Bot, Dispatcher]:
    """Construct and return a Bot and Dispatcher instance."""
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp