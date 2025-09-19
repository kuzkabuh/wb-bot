from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from app.core.config import settings
from app.core.redis import redis
from app.db.base import SessionLocal
from app.db.models import User, UserCredentials
from app.security.crypto import decrypt_value
from app.integrations.wb import get_seller_info, get_account_balance, WBError
import secrets
import json

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
    # Build buttons
    kb.button(text="Метрики")
    kb.button(text="Поставки")
    kb.button(text="Отчёты")
    kb.button(text="Профиль")
    kb.button(text="Настройки")
    kb.button(text="Проверка токена")
    # Arrange buttons into two rows of three
    kb.adjust(3, 3)
    await m.answer(
        "Привет! Я Kuzka Seller Bot.\nВыбирай раздел:",
        reply_markup=kb.as_markup(resize_keyboard=True),
    )


@router.message(F.text == "Метрики")
async def metrics(m: Message) -> None:
    """Send placeholder metrics information."""
    await m.answer("Дайджест: сегодня 0 продаж, выручка 0 ₽ (демо).")


@router.message(F.text == "Поставки")
async def supplies(m: Message) -> None:
    """Send placeholder supply recommendations."""
    await m.answer("Рекомендации по поставкам появятся после синхронизации (демо).")


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
    """Display the seller profile and balance fetched from Wildberries."""
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
    cache_bal = f"wb:balance:{m.from_user.id}"

    try:
        raw = await redis.get(cache_info)
        seller_info = json.loads(raw) if raw else await get_seller_info(token)
        if not raw:
            await redis.setex(cache_info, 55, json.dumps(seller_info, ensure_ascii=False))
    except WBError as e:
        return await m.answer(f"Ошибка WB seller-info: {e}")
    except Exception as e:
        return await m.answer(f"Ошибка seller-info: {e}")

    try:
        raw = await redis.get(cache_bal)
        balance = json.loads(raw) if raw else await get_account_balance(token)
        if not raw:
            await redis.setex(cache_bal, 55, json.dumps(balance, ensure_ascii=False))
    except WBError as e:
        return await m.answer(f"Ошибка WB balance: {e}")
    except Exception as e:
        return await m.answer(f"Ошибка balance: {e}")

    name = seller_info.get("name") or seller_info.get("supplierName") or "—"
    acc_id = (
        seller_info.get("id")
        or seller_info.get("accountId")
        or seller_info.get("supplierId")
        or "—"
    )
    bal_value = (
        balance.get("balance")
        or balance.get("currentBalance")
        or balance.get("total")
    )

    text = f"👤 Продавец: {name} \nID аккаунта: {acc_id} "
    if isinstance(bal_value, (int, float, str)):
        text += f"\n\n💰 Баланс: {bal_value} "
    else:
        text += f"\n\n💰 Баланс: формат не распознан (ключи: {', '.join(list(balance.keys())[:6])})"

    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)


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
    await m.answer("\n".join(lines))


def build_bot() -> tuple[Bot, Dispatcher]:
    """Construct and return a Bot and Dispatcher instance."""
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp