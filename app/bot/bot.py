# app/bot/bot.py
from __future__ import annotations

import os
import time
import json
import secrets
import hashlib
import asyncio
from datetime import datetime, date, timedelta
from typing import Tuple, Optional, Any, Dict, List

import httpx
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
# существующие интеграции (работают уже сейчас)
from app.integrations.wb import (
    get_seller_info,
    get_account_balance_cached,           # баланс с кэшем и нормализацией
    get_nm_report_detail,                 # detail (страница или все страницы)
    get_nm_report_detail_history,         # история по дням для nmIDs
    get_nm_report_grouped_history,        # история по дням сгруппированная
    WBError,
    ping_token,
)
# для динамических вызовов новых отчётов (реализуем после)
import app.integrations.wb as wb_integration

# -------------------------------------------------
# Router
# -------------------------------------------------
router = Router()

ANALYTICS_API = "https://seller-analytics-api.wildberries.ru"
USER_AGENT = "KuzkaSellerBot/1.0 (+wb)"

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
    kb.button(text="Поисковые запросы")
    kb.button(text="Отчёты (API)")
    kb.button(text="Дашборд")
    kb.button(text="Назад")
    kb.adjust(2, 2, 2)
    return kb.as_markup(resize_keyboard=True)


def build_funnel_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Итоги (7 дней)")
    kb.button(text="По дням (топ-5)")
    kb.button(text="Группы (бренды, 7 дней)")
    kb.button(text="Назад")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)


def build_reports_api_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Остатки на складах")
    kb.button(text="Товары с маркировкой")
    kb.button(text="Удержания")
    kb.button(text="Платная приёмка")
    kb.button(text="Платное хранение")
    kb.button(text="Продажи по регионам")
    kb.button(text="Доля бренда в продажах")
    kb.button(text="Скрытые товары")
    kb.button(text="Возвраты и перемещения")
    kb.button(text="Назад к отчётам")
    kb.adjust(2, 2, 2, 2, 2)
    return kb.as_markup(resize_keyboard=True)


def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    s = f"{x:,.2f}".replace(",", " ")
    return s


def _fmt_int(x: Any) -> str:
    try:
        n = int(float(x))
    except Exception:
        return "0"
    return f"{n:,}".replace(",", " ")


def _pick_balance_fields(bal: dict) -> tuple[Optional[float], Optional[float], str]:
    total = (
        bal.get("total")
        if bal.get("total") is not None
        else bal.get("current") or bal.get("currentBalance") or bal.get("balance")
    )
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


async def _require_user_and_token(m: Message) -> tuple[Optional[User], Optional[str], Optional[InlineKeyboardMarkup]]:
    """
    Возвращает (user, token, keyboard_for_login_if_needed)
    """
    with SessionLocal() as db:
        user = db.query(User).filter(User.tg_id == m.from_user.id).first()
        if not user:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Открыть кабинет", url=login_url)]]
            )
            return None, None, ikb

        cred = db.query(UserCredentials).filter_by(user_id=user.id).first()
        if not cred:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Сохранить API-ключ", url=login_url)]]
            )
            return None, None, ikb

        try:
            token = decrypt_value(cred.wb_api_key_encrypted)
        except Exception:
            login_url = await build_login_url(m.from_user.id)
            ikb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Обновить API-ключ", url=login_url)]]
            )
            return user, None, ikb

    return user, token, None


def _now_dt_strings_for_detail(days: int = 7) -> tuple[str, str]:
    """
    Для /nm-report/detail нужны YYYY-MM-DD HH:MM:SS.
    Начало — 00:00:00 даты (now - days), конец — 23:59:59 сегодняшней даты.
    """
    now = datetime.utcnow()
    begin_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    period_begin = f"{begin_date} 00:00:00"
    period_end = f"{end_date} 23:59:59"
    return period_begin, period_end


def _date_range_for_history(days: int = 7) -> tuple[str, str]:
    """
    Для /detail/history и /grouped/history нужны YYYY-MM-DD.
    """
    today = date.today()
    begin = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    return begin, end


async def _analytics_rate_limit(m: Message, cooldown_sec: int = 20) -> bool:
    """
    Простейший локальный лимитер: не чаще одного запроса аналитики
    в 20 сек/пользователя. True — можно, False — рано.
    """
    key = f"wb:analytics:last:{m.from_user.id}"
    try:
        last_raw = await redis.get(key)
    except Exception:
        last_raw = None

    now_ts = int(time.time())
    if last_raw:
        try:
            last_ts = int(last_raw)
            if now_ts - last_ts < cooldown_sec:
                wait = cooldown_sec - (now_ts - last_ts)
                await m.answer(f"Слишком часто. Подождите ещё {wait} с и повторите.")
                return False
        except Exception:
            pass

    try:
        await redis.set(key, str(now_ts))
    except Exception:
        pass
    return True


# ---------------- Analytics HTTP helper (для старых эндпоинтов в этом файле) ---------------
async def _analytics_post(token: str, path: str, payload: dict, timeout: float = 20.0) -> Any:
    headers = {
        "Authorization": token,         # токен аналитики в хедере
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    url = f"{ANALYTICS_API}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code == 401:
        raise WBError("401 Unauthorized (проверьте токен аналитики)")
    if r.status_code == 429:
        raise WBError("429 Too Many Requests (лимит WB аналитики)")
    if r.status_code >= 400:
        raise WBError(f"{r.status_code} {r.text}")
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise WBError(f"Некорректный JSON от аналитики: {e}")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


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


# ------------------- Воронка: меню -------------------
@router.message(F.text == "Воронка продаж")
async def funnel_menu(m: Message) -> None:
    await m.answer("Воронка продаж — выберите режим:", reply_markup=build_funnel_menu())


# ------------------- Воронка: Итоги (7 дней) -------------------
@router.message(F.text == "Итоги (7 дней)")
async def funnel_summary(m: Message) -> None:
    if not await _analytics_rate_limit(m):
        return

    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ аналитики.", reply_markup=ikb, disable_web_page_preview=True)

    # detail: требуются YYYY-MM-DD HH:MM:SS
    period_begin, period_end = _now_dt_strings_for_detail(days=7)
    tz = "Europe/Moscow"  # WB default

    try:
        data = await get_nm_report_detail(
            token,
            period_begin,
            period_end,
            timezone=tz,
            page=1,
            order_by={"field": "orders", "mode": "desc"},
        )
    except WBError as e:
        return await m.answer(f"Ошибка аналитики: {e}", reply_markup=build_funnel_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт: {e}", reply_markup=build_funnel_menu())

    # достаём список карточек
    cards: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        for key in ("cards", "cardAnaliticsData", "analyticsData", "items", "rows"):
            if isinstance(data.get(key), list):
                cards = data[key]
                break
    elif isinstance(data, list):
        cards = data

    # агрегируем суммы по странице
    s_open = sum(int(c.get("openCard") or 0) for c in cards)
    s_cart = sum(int(c.get("addToCart") or 0) for c in cards)
    s_orders = sum(int(c.get("orders") or c.get("ordersCount") or 0) for c in cards)

    # топ-10 по заказам
    top = sorted(cards, key=lambda c: int(c.get("orders") or c.get("ordersCount") or 0), reverse=True)[:10]

    lines = [
        f"Итоги за {period_begin} – {period_end}",
        f"Переходы: {_fmt_int(s_open)}; В корзину: {_fmt_int(s_cart)}; Заказы: {_fmt_int(s_orders)}",
        "",
        "Топ-10 карточек по заказам:",
    ]
    for c in top:
        nm_id = c.get("nmId") or c.get("nmID") or c.get("article") or "?"
        oc = _fmt_int(c.get("openCard") or 0)
        ac = _fmt_int(c.get("addToCart") or 0)
        od = _fmt_int(c.get("orders") or c.get("ordersCount") or 0)
        lines.append(f"• {nm_id}: переходы={oc}, корзина={ac}, заказы={od}")

    await m.answer("\n".join(lines), reply_markup=build_funnel_menu())


# ------------------- Воронка: По дням (топ-5) -------------------
@router.message(F.text == "По дням (топ-5)")
async def funnel_daily_top5(m: Message) -> None:
    if not await _analytics_rate_limit(m):
        return

    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ аналитики.", reply_markup=ikb, disable_web_page_preview=True)

    # 1) Берём топ nmIDs из detail (нужны datetime-строки)
    detail_begin, detail_end = _now_dt_strings_for_detail(days=7)
    tz = "Europe/Moscow"

    try:
        data = await get_nm_report_detail(
            token,
            detail_begin,
            detail_end,
            timezone=tz,
            page=1,
            order_by={"field": "orders", "mode": "desc"},
        )
    except Exception as e:
        return await m.answer(f"Не удалось получить список карточек: {e}", reply_markup=build_funnel_menu())

    cards: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        for key in ("cards", "cardAnaliticsData", "analyticsData", "items", "rows"):
            if isinstance(data.get(key), list):
                cards = data[key]
                break
    elif isinstance(data, list):
        cards = data

    ranked = sorted(cards, key=lambda c: int(c.get("orders") or c.get("ordersCount") or 0), reverse=True)
    nm_ids: List[int] = []
    for c in ranked:
        nm = c.get("nmId") or c.get("nmID")
        if nm and nm not in nm_ids:
            try:
                nm_ids.append(int(nm))
            except Exception:
                continue
        if len(nm_ids) >= 5:
            break

    if not nm_ids:
        return await m.answer("Не нашёл карточек для отчёта.", reply_markup=build_funnel_menu())

    # 2) detail/history для топ-5 nmIDs (нужны date-строки)
    hist_begin, hist_end = _date_range_for_history(days=7)
    try:
        hist = await get_nm_report_detail_history(
            token,
            nm_ids=nm_ids,
            period_begin=hist_begin,
            period_end=hist_end,
            timezone=tz,
            aggregation_level="day",
        )
    except WBError as e:
        return await m.answer(f"Ошибка аналитики (history): {e}", reply_markup=build_funnel_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить историю: {e}", reply_markup=build_funnel_menu())

    # ожидаем массив объектов по дням/sku — аккуратно агрегируем по nmID
    per_nm: Dict[int, Dict[str, int]] = {nm: {"openCard": 0, "addToCart": 0, "orders": 0} for nm in nm_ids}
    rows = hist if isinstance(hist, list) else (hist.get("data") if isinstance(hist, dict) else [])
    if isinstance(rows, list):
        for row in rows:
            nm = row.get("nmId") or row.get("nmID")
            if not nm:
                continue
            try:
                nm = int(nm)
            except Exception:
                continue
            per_nm.setdefault(nm, {"openCard": 0, "addToCart": 0, "orders": 0})
            per_nm[nm]["openCard"] += int(row.get("openCard") or 0)
            per_nm[nm]["addToCart"] += int(row.get("addToCart") or 0)
            per_nm[nm]["orders"] += int(row.get("orders") or row.get("ordersCount") or 0)

    lines = [f"По дням (7 дней): топ-5 SKU — {hist_begin}…{hist_end}"]
    for nm in nm_ids:
        mtr = per_nm.get(nm, {})
        lines.append(
            f"• {nm}: переходы={_fmt_int(mtr.get('openCard', 0))}, "
            f"корзина={_fmt_int(mtr.get('addToCart', 0))}, "
            f"заказы={_fmt_int(mtr.get('orders', 0))}"
        )

    await m.answer("\n".join(lines), reply_markup=build_funnel_menu())


# ------------------- Воронка: Группы (бренды, 7 дней) -------------------
@router.message(F.text == "Группы (бренды, 7 дней)")
async def funnel_grouped_brands(m: Message) -> None:
    if not await _analytics_rate_limit(m):
        return

    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ аналитики.", reply_markup=ikb, disable_web_page_preview=True)

    hist_begin, hist_end = _date_range_for_history(days=7)
    tz = "Europe/Moscow"

    try:
        data = await get_nm_report_grouped_history(
            token,
            period_begin=hist_begin,
            period_end=hist_end,
            object_ids=[],
            brand_names=[],
            tag_ids=[],
            timezone=tz,
            aggregation_level="day",
        )
    except WBError as e:
        return await m.answer(f"Ошибка grouped/history: {e}", reply_markup=build_funnel_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить grouped/history: {e}", reply_markup=build_funnel_menu())

    # Ожидаем список записей по дням и брендам — аккуратно суммируем по brandName
    per_brand: Dict[str, Dict[str, int]] = {}
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
    if isinstance(rows, list):
        for row in rows:
            brand = row.get("brandName") or row.get("brand") or "Без бренда"
            rec = per_brand.setdefault(brand, {"openCard": 0, "addToCart": 0, "orders": 0})
            rec["openCard"] += int(row.get("openCard") or 0)
            rec["addToCart"] += int(row.get("addToCart") or 0)
            rec["orders"] += int(row.get("orders") or row.get("ordersCount") or 0)

    top_brands = sorted(per_brand.items(), key=lambda kv: kv[1].get("orders", 0), reverse=True)[:10]

    lines = [f"Группы по брендам за {hist_begin}…{hist_end} (топ-10):"]
    if not top_brands:
        lines.append("Данных по брендам не найдено.")
    else:
        for name, mtr in top_brands:
            lines.append(
                f"• {name}: переходы={_fmt_int(mtr['openCard'])}, корзина={_fmt_int(mtr['addToCart'])}, заказы={_fmt_int(mtr['orders'])}"
            )

    await m.answer("\n".join(lines), reply_markup=build_funnel_menu())


# ------------------- Поисковые запросы (14 дней) -------------------
@router.message(F.text == "Поисковые запросы")
async def search_queries_report(m: Message) -> None:
    """
    Топ поисковых запросов по товарам продавца за последние 14 дней
    с включёнными searchTexts. Сравнение с прошлым периодом такого же размера.
    """
    if not await _analytics_rate_limit(m):
        return

    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ аналитики (категория Аналитика).", reply_markup=ikb, disable_web_page_preview=True)

    # Периоды: current — последние 14 дней, past — предыдущие 14
    today = date.today()
    cur_begin = (today - timedelta(days=14)).isoformat()
    cur_end = today.isoformat()
    past_end_d = (today - timedelta(days=14))
    past_begin_d = (today - timedelta(days=28))
    past_begin = past_begin_d.isoformat()
    past_end = past_end_d.isoformat()

    payload = {
        "currentPeriod": {"start": cur_begin, "end": cur_end},
        "pastPeriod": {"start": past_begin, "end": past_end},
        "nmIds": [],
        "subjectIds": [],
        "brandNames": [],
        "tagIds": [],
        "timezone": "Europe/Moscow",
        "orderBy": {"field": "orders", "mode": "desc"},
        "positionCluster": "ALL",
        "includeSubstitutedSKUs": True,
        "includeSearchTexts": True,
        "limit": 20,
        "offset": 0,
    }

    try:
        data = await _analytics_post(token, "/api/v2/search-report/report", payload)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта по поисковым запросам: {e}")
    except Exception as e:
        return await m.answer(f"Не удалось получить поисковые запросы: {e}")

    groups = []
    if isinstance(data, dict):
        if isinstance(data.get("groups"), list):
            groups = data["groups"]
        elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("groups"), list):
            groups = data["data"]["groups"]
    elif isinstance(data, list):
        groups = data

    def _freq_weight(val: Any) -> int:
        if val is None:
            return 0
        try:
            return int(float(val))
        except Exception:
            pass
        s = str(val).strip().upper()
        mapping = {"LOW": 1, "MEDIUM": 5, "HIGH": 10, "VERY_HIGH": 15, "VERYHIGH": 15}
        return mapping.get(s, 1)

    text_weights: Dict[str, int] = {}
    total_orders = 0
    for g in groups:
        stat = g.get("sellingAggregationStat") or g.get("sellingTableStat") or {}
        total_orders += int(stat.get("orders") or 0)
        st = g.get("searchTexts") or []
        if isinstance(st, list):
            for it in st:
                txt = (it.get("text") or "").strip()
                if not txt:
                    continue
                w = _freq_weight(it.get("frequency") or it.get("count") or 1)
                text_weights[txt] = text_weights.get(txt, 0) + w

    top_texts = sorted(text_weights.items(), key=lambda kv: kv[1], reverse=True)[:15]

    header = (
        "🔎 Поисковые запросы (14 дней)\n"
        f"Текущий период: {cur_begin} – {cur_end}\n"
        f"Прошлый период: {past_begin} – {past_end}\n"
        f"Заказы по группам (сумма): {_fmt_int(total_orders)}\n"
    )

    if not top_texts:
        return await m.answer(header + "\nНет данных по поисковым запросам.")

    lines = [header, "Топ запросов:"]
    for txt, w in top_texts:
        lines.append(f"• {txt} — {_fmt_int(w)}")
    lines.append("\nПодсказка: скоро добавим детализацию по группам и товарам.")

    await m.answer("\n".join(lines), reply_markup=build_reports_menu())


# ======================= НОВЫЕ ОТЧЁТЫ (WB Reports API) =======================

def _period_last_days(days: int) -> tuple[str, str]:
    """YYYY-MM-DD для большинства отчётов из Reports API (если требуется период)."""
    today = date.today()
    begin = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    return begin, end


async def _call_report(func_name: str, token: str, **kwargs) -> Any:
    """
    Безопасный вызов функций интеграции (которые добавим в app/integrations/wb.py).
    Если функция ещё не реализована — отдаём дружелюбную ошибку.
    """
    func = getattr(wb_integration, func_name, None)
    if not callable(func):
        raise WBError(f"Интеграция не реализована: {func_name}")
    return await func(**kwargs)


def _preview_table(rows: List[Dict[str, Any]], keys_priority: List[str], limit: int = 10) -> List[str]:
    """
    Универсальный предпросмотр первых N строк. Ищет в ряду полезные ключи по приоритету,
    строит короткую строку. Если ключей нет — печатает весь ряд в компактном JSON.
    """
    out: List[str] = []
    for i, r in enumerate(rows[:limit]):
        parts: List[str] = []
        for k in keys_priority:
            if k in r and r.get(k) not in (None, "", [], {}):
                val = r[k]
                if isinstance(val, float):
                    parts.append(f"{k}={_fmt_money(val)}")
                else:
                    parts.append(f"{k}={val}")
        if not parts:
            try:
                parts.append(json.dumps(r, ensure_ascii=False)[:140])
            except Exception:
                parts.append(str(r)[:140])
        out.append(f"{i+1}. " + ", ".join(parts))
    return out


@router.message(F.text == "Отчёты (API)")
async def reports_api_menu(m: Message) -> None:
    await m.answer("Отчёты WB (API). Выберите нужный:", reply_markup=build_reports_api_menu())


# --------- Остатки на складах
@router.message(F.text == "Остатки на складах")
async def report_stocks(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)

    # многие «остаточные» отчёты — срез на текущий момент (без периода)
    try:
        data = await _call_report("get_report_stocks", token=token)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Остатки: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Остатки: {e}", reply_markup=build_reports_api_menu())

    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("stocks", "items", "rows", "list", "data"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break

    total_qty = sum(int(r.get("quantity") or r.get("qty") or 0) for r in rows)
    lines = [f"📦 Остатки на складах: всего строк: {_fmt_int(len(rows))}, суммарно шт.: {_fmt_int(total_qty)}"]
    preview = _preview_table(rows, ["warehouseName", "supplierArticle", "nmID", "quantity", "qty", "size"])
    if preview:
        lines.append("Топ записей:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Товары с обязательной маркировкой
@router.message(F.text == "Товары с маркировкой")
async def report_marking(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    try:
        data = await _call_report("get_report_marking", token=token)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Маркировка: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Маркировка: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("items") or data.get("rows") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    lines = [f"🏷️ Товары с обязательной маркировкой: {_fmt_int(len(rows))} позиций."]
    preview = _preview_table(rows, ["supplierArticle", "nmID", "cis", "status", "warehouseName"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Удержания
@router.message(F.text == "Удержания")
async def report_withholdings(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_withholdings", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Удержания: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Удержания: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    amount = sum(float(r.get("amount") or r.get("sum") or 0) for r in rows)
    lines = [f"⛔ Удержания за {begin}–{end}: {_fmt_money(amount)} ₽, строк: {_fmt_int(len(rows))}"]
    preview = _preview_table(rows, ["type", "reason", "docNumber", "amount"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Платная приёмка
@router.message(F.text == "Платная приёмка")
async def report_paid_acceptance(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_paid_acceptance", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Платная приёмка: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Платная приёмка: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    amount = sum(float(r.get("amount") or r.get("sum") or 0) for r in rows)
    lines = [f"📥 Платная приёмка за {begin}–{end}: {_fmt_money(amount)} ₽, строк: {_fmt_int(len(rows))}"]
    preview = _preview_table(rows, ["docDate", "warehouseName", "count", "amount"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Платное хранение
@router.message(F.text == "Платное хранение")
async def report_paid_storage(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_paid_storage", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Платное хранение: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Платное хранение: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    amount = sum(float(r.get("amount") or r.get("sum") or 0) for r in rows)
    lines = [f"🏬 Платное хранение за {begin}–{end}: {_fmt_money(amount)} ₽, строк: {_fmt_int(len(rows))}"]
    preview = _preview_table(rows, ["period", "warehouseName", "volume", "amount"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Продажи по регионам
@router.message(F.text == "Продажи по регионам")
async def report_sales_regions(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_sales_by_regions", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Продажи по регионам: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Продажи по регионам: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    revenue = sum(float(r.get("revenue") or r.get("sum") or 0) for r in rows)
    lines = [f"🗺️ Продажи по регионам за {begin}–{end}: выручка {_fmt_money(revenue)} ₽, строк: {_fmt_int(len(rows))}"]
    preview = _preview_table(rows, ["region", "orders", "revenue"])
    if preview:
        lines.append("Топ регионов:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Доля бренда в продажах
@router.message(F.text == "Доля бренда в продажах")
async def report_brand_share(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_brand_share", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Доля бренда: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Доля бренда: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    lines = [f"🏷️ Доля бренда в продажах за {begin}–{end}:"]
    preview = _preview_table(rows, ["brandName", "orders", "revenue", "share"])
    if preview:
        lines.extend(["• " + p for p in preview])
    else:
        lines.append(f"Всего брендов: {_fmt_int(len(rows))}")
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Скрытые товары
@router.message(F.text == "Скрытые товары")
async def report_hidden_goods(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    try:
        data = await _call_report("get_report_hidden_goods", token=token)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Скрытые товары: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Скрытые товары: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    lines = [f"🙈 Скрытые товары: {_fmt_int(len(rows))} позиций."]
    preview = _preview_table(rows, ["nmID", "supplierArticle", "reason", "date"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


# --------- Возвраты и перемещения
@router.message(F.text == "Возвраты и перемещения")
async def report_returns_transfers(m: Message) -> None:
    user, token, ikb = await _require_user_and_token(m)
    if not token:
        return await m.answer("Нужен API-ключ для отчётов.", reply_markup=ikb, disable_web_page_preview=True)
    begin, end = _period_last_days(30)
    try:
        data = await _call_report("get_report_returns_transfers", token=token, date_begin=begin, date_end=end)
    except WBError as e:
        return await m.answer(f"Ошибка отчёта Возвраты/перемещения: {e}", reply_markup=build_reports_api_menu())
    except Exception as e:
        return await m.answer(f"Не удалось получить отчёт Возвраты/перемещения: {e}", reply_markup=build_reports_api_menu())

    rows = data if isinstance(data, list) else (data.get("rows") or data.get("items") or data.get("data") or [])
    if not isinstance(rows, list):
        rows = []
    cnt_returns = sum(int(r.get("returns") or r.get("count") or 0) for r in rows)
    lines = [f"↩️ Возвраты и перемещения за {begin}–{end}: записей {_fmt_int(len(rows))}, возвратов {_fmt_int(cnt_returns)}"]
    preview = _preview_table(rows, ["nmID", "supplierArticle", "type", "count", "warehouseName", "date"])
    if preview:
        lines.append("Примеры:")
        lines.extend(["• " + p for p in preview])
    await m.answer("\n".join(lines), reply_markup=build_reports_api_menu())


@router.message(F.text == "Назад к отчётам")
async def back_to_reports(m: Message) -> None:
    await reports_menu(m)


# -------------------------------------------------
# Дашборд ссылка
# -------------------------------------------------
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
            if str(val).lower().strip() == "ok":
                lines.append(f"✅ {name}")
            else:
                lines.append(f"❌ {name}: {val}")

    await m.answer("\n".join(lines), reply_markup=build_profile_menu())


@router.message(F.text == "Баланс")
async def show_balance(m: Message) -> None:
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
