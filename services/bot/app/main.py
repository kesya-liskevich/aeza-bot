# app/main.py — Telegram bot (aiogram)
# -*- coding: utf-8 -*-
import os
import re
import json
import math
import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Optional

import aiohttp
from aiohttp import web

import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import (
    TelegramMigrateToChat,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from redis.asyncio import Redis

from aiogram.client.default import DefaultBotProperties

from datetime import date, timedelta

from aiogram.types import FSInputFile


# Глобальный кэш городов ATI
ATI_CITY_CACHE = []


# ===================== Конфиг =====================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
MANAGER_GROUP_ID = int(os.environ.get("MANAGER_GROUP_ID", "0"))
API_BASE = os.environ.get("API_BASE_URL", "http://api:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
BOT_PUBLIC_URL = os.environ.get("BOT_PUBLIC_URL", "").strip()

# Опциональная тема «входящие» для групп-форумов
_env_inbox = os.environ.get("MANAGER_TOPIC_INBOX", "").strip()
TOPIC_INBOX = int(_env_inbox) if _env_inbox.lstrip("-").isdigit() else None

# OpenAI (грубая оценка ставки)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_RATE_MODEL = os.getenv("GPT_RATE_MODEL", "gpt-4o-mini")
try:
    from openai import AsyncOpenAI
    oai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    oai_client = None

# Транспорт
bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
router = Router()
dp.include_router(router)

redis = Redis.from_url(REDIS_URL, decode_responses=True)

# Redis keys
R_INBOX_TOPIC = "topics:inbox"
USER_TMP_STACK = "tmpmsgs:{uid}"  # список message_id, чтобы чистить «хвосты»

# --- Redis key templates ---
THREAD_TO_CLIENT = "thread_to_client:{tid}"
CLIENT_TO_THREAD = "client_to_thread:{uid}"

# ===================== Доменные модели =====================

@dataclass
class QuoteDraft:
    # выбор формата
    cargo_format: Optional[str] = None  # general | container | oversize

    # container
    container_hc: Optional[bool] = None
    container_volume: Optional[int] = None
    container_return_empty: Optional[bool] = None

    # general
    truck_class: Optional[str] = None   # 0.8/1.5/3/5/10/20 (тонн)
    volume_bucket: Optional[str] = None # «3-5», «20-30» (м³)
    ftl_ltl: Optional[str] = None       # ftl | ltl

    # oversize
    length_m: Optional[float] = None
    width_m: Optional[float] = None
    height_m: Optional[float] = None
    weight_kg: Optional[int] = None

    # общее
    route_from: Optional[str] = None
    route_to: Optional[str] = None
    loading: Optional[str] = None       # side|rear|top|unknown

    # === ATI calculation fields ===
    car_types: Optional[list[str]] = None
    tonnage: Optional[float] = None
    with_nds: Optional[list[bool]] = None

    # результаты/намерение
    avg_rate: Optional[int] = None
    intent: Optional[str] = None

    # простая форма
    cargo_text: Optional[str] = None
    weight_text: Optional[str] = None
    volume_text: Optional[str] = None
    quote_id: Optional[int] = None


# ===================== Форматирование =====================

RUB = "₽"

def fmt_rub(n: Optional[int | float]) -> str:
    if n is None:
        return "—"
    try:
        n = int(round(float(n)))
    except Exception:
        return "—"
    s = f"{n:,}".replace(",", " ")
    return f"{s} {RUB}"

def render_application(d: QuoteDraft, rate_rub: Optional[int], user_name: str = "", user_id: Optional[int] = None) -> str:
    """Единый красивый шаблон карточки заявки (для клиента, предпросмотра и менеджеров)."""
    cargo_label = {
        "general": "Обычный (ТНП, паллеты)",
        "container": "Контейнер",
        "oversize": "Негабарит",
        None: "-",
    }.get(d.cargo_format, "-")

    rows: list[str] = []

    # Заголовок с номером заявки, если он есть
    quote_id = getattr(d, "quote_id", None)
    if quote_id:
        rows.append(f"📝 **Заявка #{quote_id}**")
    else:
        rows.append("📝 **Заявка на перевозку**")

    # Строка с клиентом
    meta = []
    if user_name:
        meta.append(user_name)
    if user_id is not None:
        meta.append(f"TG ID {user_id}")
    if meta:
        rows.append("_Клиент: " + " • ".join(meta) + "_")


    rows.append("")
    rows.append(f"**Маршрут:** {d.route_from or '—'} → {d.route_to or '—'}")
    rows.append(f"**Формат:** {cargo_label}")

    if d.cargo_format == "container":
        rows.append(f"**Контейнер:** {d.container_volume or '—'} ft, HC={'да' if d.container_hc else 'нет'}")
        rows.append(f"**Возврат пустого:** {'да' if d.container_return_empty else 'нет'}")

    if d.cargo_format == "general":
        rows.append(f"**Машина:** {d.truck_class or '—'} т")
        rows.append(f"**Объём:** {d.volume_bucket or '—'} м³")
        rows.append(f"**Режим:** {'FTL (отдельная)' if d.ftl_ltl == 'ftl' else 'LTL (догруз)'}")

    if d.cargo_format == "oversize":
        dims = f"{d.length_m or '—'} × {d.width_m or '—'} × {d.height_m or '—'} м"
        rows.append(f"**Негабарит:** {dims}, {d.weight_kg or '—'} кг")

    rows.append("")
    rows.append("💰 **Оценка ставки:** " + ("от " + fmt_rub(rate_rub) if rate_rub else "—"))
    rows.append("")
    rows.append("ℹ️ Это ориентировочная ставка. Для точного расчёта подключим логиста.")
    return "\n".join(rows)

def render_simple_calc_application(
    d: QuoteDraft,
    rate_rub: Optional[int],
    user_name: str = "",
    user_id: Optional[int] = None,
) -> str:
    """
    Простой шаблон для новой линейки:
    только груз / адреса / вес / объём и, опционально, ставка.
    """
    quote_id = getattr(d, "quote_id", None)
    if quote_id:
        rows = [f"📝 Просчёт перевозки #{quote_id}"]
    else:
        rows = ["📝 Просчёт перевозки"]

    # строка с клиентом
    meta = []
    if user_name:
        meta.append(user_name)
    if user_id is not None:
        meta.append(f"TG ID {user_id}")
    if meta:
        rows.append("Клиент: " + " • ".join(meta))

    rows.append("")

    # сами поля заявки – только если заполнены
    if d.cargo_text:
        rows.append(f"Груз: {d.cargo_text}")
    if d.route_from:
        rows.append(f"Адрес погрузки: {d.route_from}")
    if d.route_to:
        rows.append(f"Адрес выгрузки: {d.route_to}")
    if d.weight_text:
        rows.append(f"Вес: {d.weight_text}")
    if d.volume_text:
        rows.append(f"Объём: {d.volume_text}")

    # блок ставки – только на финальном шаге
    if rate_rub is not None:
        rows.append("")
        rows.append("💰 Оценка ставки: от " + fmt_rub(rate_rub))
        rows.append("ℹ️ Это ориентировочная ставка. Для точного расчёта подключим логиста.")

    return "\n".join(rows)

# ===================== ATI: настройки API =====================

ATI_API_BASE_URL = "https://api.ati.su"
ATI_AVERAGE_PRICES_URL = f"{ATI_API_BASE_URL}/priceline/license/v1/average_prices"
ATI_ALL_DIRECTIONS_URL = f"{ATI_API_BASE_URL}/priceline/license/v1/all_directions"

# Токен ATI: можно назвать ATI_API_TOKEN или ATI_TOKEN — берём любой
ATI_API_TOKEN = os.getenv("ATI_API_TOKEN") or os.getenv("ATI_TOKEN") or ""

# демо-режим, чтобы можно было тестировать без боевой лицензии
ATI_USE_DEMO = False

# небольшой кэш городов ATI: "moskva" -> 1 и т.п.
_ATI_CITY_CACHE: dict[str, int] = {}
_ATI_CITY_CACHE_LOADED = False


# ===================== Вспомогательное =====================

async def _get_inbox_thread_id() -> Optional[int]:
    if TOPIC_INBOX:
        return TOPIC_INBOX
    val = await redis.get(R_INBOX_TOPIC)
    try:
        return int(val) if val else None
    except Exception:
        return None

async def send_tmp(m: Message, text: str, **kwargs) -> Message:
    msg = await m.answer(text, **kwargs)
    key = USER_TMP_STACK.format(uid=m.from_user.id)
    await redis.rpush(key, msg.message_id)
    return msg

async def send_tmp_by_id(chat_id: int, text: str, **kwargs) -> Message:
    msg = await bot.send_message(chat_id, text, **kwargs)
    key = USER_TMP_STACK.format(uid=chat_id)
    await redis.rpush(key, msg.message_id)
    return msg

from aiogram.types import FSInputFile, Message

async def send_tmp_photo(
    m: Message,
    photo_path: str,
    caption: str | None = None,
    **kwargs,
) -> Message:
    photo = FSInputFile(photo_path)
    msg = await m.answer_photo(photo, caption=caption, **kwargs)

    key = USER_TMP_STACK.format(uid=m.from_user.id)
    await redis.rpush(key, msg.message_id)

    return msg


async def send_tmp_photo_by_user_id(
    user_id: int,
    photo_path: str,
    caption: str | None = None,
    **kwargs,
) -> Message:
    photo = FSInputFile(photo_path)
    msg = await bot.send_photo(
        chat_id=user_id,
        photo=photo,
        caption=caption,
        **kwargs,
    )

    key = USER_TMP_STACK.format(uid=user_id)
    await redis.rpush(key, msg.message_id)

    return msg


async def ensure_quote_header(user_id: int, state: FSMContext) -> None:
    """Создаёт/обновляет шапку заявки над диалогом."""
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))

    # если номера ещё нет — сгенерировать простой id
    if draft.quote_id is None:
        draft.quote_id = int(time.time())  # можно потом заменить на свою схему
        await state.update_data(draft=asdict(draft))
        data = await state.get_data()  # обновлённые данные

    header_id = data.get("quote_header_id")

    # Строим текст: только заполненные поля
    lines = [f"📝 Просчёт перевозки #{draft.quote_id}"]

    if draft.cargo_text:
        lines.append(f"Груз: {draft.cargo_text}")
    if draft.route_from:
        lines.append(f"Адрес погрузки: {draft.route_from}")
    if draft.route_to:
        lines.append(f"Адрес выгрузки: {draft.route_to}")
    if draft.weight_text:
        lines.append(f"Вес: {draft.weight_text}")
    if draft.volume_text:
        lines.append(f"Объём: {draft.volume_text}")

    text = "\n".join(lines)

    if header_id:
        # обновляем существующую шапку
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=header_id,
                text=text,
            )
        except Exception:
            # если вдруг сообщение удалили — создаём заново
            header_id = None

    if not header_id:
        msg = await bot.send_message(user_id, text)
        await state.update_data(quote_header_id=msg.message_id)



async def clean_tmp(user_id: int, keep_last: int = 0):
    key = USER_TMP_STACK.format(uid=user_id)
    ids = await redis.lrange(key, 0, -1)
    if not ids:
        return
    if keep_last > 0:
        ids_to_delete = ids[:-keep_last]
        keep = ids[-keep_last:]
    else:
        ids_to_delete, keep = ids, []
    for mid in ids_to_delete:
        try:
            await bot.delete_message(chat_id=user_id, message_id=int(mid))
        except Exception:
            pass
    await redis.delete(key)
    for mid in keep:
        await redis.rpush(key, mid)

# ===================== Состояния =====================

class Flow(StatesGroup):
    JUST_ASK_INPUT = State()
    CARGO_FORMAT = State()

    CONTAINER_TYPE = State()
    CONTAINER_VOLUME = State()
    CONTAINER_RETURN = State()

    PALLETS_WEIGHT = State()
    PALLETS_VOLUME = State()
    FTL_LTL = State()

    OVERSIZE_DIMENSIONS = State()

    ROUTE_FROM = State()
    ROUTE_TO = State()
    LOADING_TYPE = State()

    REVIEW = State()
    RATE = State()

class CalcFlow(StatesGroup):
    CARGO = State()
    FROM = State()
    TO = State()
    WEIGHT = State()
    WEIGHT_CUSTOM = State()
    VOLUME = State()
    VOLUME_CUSTOM = State()
    FTL_MODE = State()
    REVIEW = State()
    EDIT_FIELD = State()
    CALCULATING = State()


class CallFlow(StatesGroup):
    CALLBACK_PHONE = State()


# ===================== Клавиатуры =====================

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сделать просчёт", callback_data="mode:calc_simple")],
        [InlineKeyboardButton(text="Задать вопрос", callback_data="mode:ask")],
        [InlineKeyboardButton(text="Телеграм-канал", url="https://t.me/aezalogistic")],
        [InlineKeyboardButton(text="Запросить звонок", callback_data="mode:call")],
    ])

def kb_step_main():
    # просто кнопка возврата в главное меню
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Главное меню", callback_data="back:menu")]
    ])


def kb_weight_simple():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="до 1,5 т", callback_data="wgt:1.5"),
            InlineKeyboardButton(text="до 3 т", callback_data="wgt:3"),
        ],
        [
            InlineKeyboardButton(text="до 5 т", callback_data="wgt:5"),
            InlineKeyboardButton(text="до 10 т", callback_data="wgt:10"),
        ],
        [
            InlineKeyboardButton(text="до 20 т", callback_data="wgt:20"),
        ],
        [
            InlineKeyboardButton(text="Другой вес", callback_data="wgt:other"),
        ],
        [
            InlineKeyboardButton(text="Главное меню", callback_data="back:menu"),
        ],
    ])


def kb_volume_simple():
    buckets = ["10", "20", "40", "90", "120"]
    rows = [[InlineKeyboardButton(text=f"до {b} м³", callback_data=f"vol:{b}")] for b in buckets]
    rows.append([InlineKeyboardButton(text="Другой объём", callback_data="vol:other")])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ftl_ltl_simple():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Отдельная машина (FTL)", callback_data="sftl:ftl"),
            InlineKeyboardButton(text="Догруз (LTL)", callback_data="sftl:ltl"),
        ],
        [
            InlineKeyboardButton(text="Главное меню", callback_data="back:menu"),
        ],
    ])

def kb_calc_review():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="calc:confirm")],
        [InlineKeyboardButton(text="Изменить", callback_data="calc:edit")],
        [InlineKeyboardButton(text="Главное меню", callback_data="back:menu")],
    ])

def kb_ask_question():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Главное меню", callback_data="back:menu")]
        ]
    )

def kb_calc_edit_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Груз", callback_data="cedit:cargo")],
        [InlineKeyboardButton(text="Адрес погрузки", callback_data="cedit:from")],
        [InlineKeyboardButton(text="Адрес выгрузки", callback_data="cedit:to")],
        [InlineKeyboardButton(text="Вес", callback_data="cedit:weight")],
        [InlineKeyboardButton(text="Объём", callback_data="cedit:volume")],
        [InlineKeyboardButton(text="Отмена", callback_data="cedit:cancel")],
    ])


def kb_back(code: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="« Назад", callback_data=f"back:{code}")
    ]])

def kb_format():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный (ТНП, паллеты)", callback_data="fmt:general")],
        [InlineKeyboardButton(text="Контейнер", callback_data="fmt:container")],
        [InlineKeyboardButton(text="Негабарит", callback_data="fmt:oversize")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:menu")],
    ])

def kb_container_type():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="High Cube", callback_data="ct:hc"),
         InlineKeyboardButton(text="Обычный", callback_data="ct:std")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:fmt")],
    ])

def kb_container_volume():
    row = [InlineKeyboardButton(text=str(x), callback_data=f"cv:{x}") for x in (20, 30, 40, 45)]
    return InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(text="« Назад", callback_data="back:ctype")]])

def kb_container_return():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Остаётся", callback_data="cr:keep"),
         InlineKeyboardButton(text="Возвращаем пустым", callback_data="cr:return")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:cvol")],
    ])

def kb_truck():
    rows = [
        [InlineKeyboardButton(text="до 800 кг", callback_data="truck:0.8"),
         InlineKeyboardButton(text="1.5 т", callback_data="truck:1.5")],
        [InlineKeyboardButton(text="3 т", callback_data="truck:3"),
         InlineKeyboardButton(text="5 т", callback_data="truck:5")],
        [InlineKeyboardButton(text="10 т", callback_data="truck:10"),
         InlineKeyboardButton(text="20 т", callback_data="truck:20")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:fmt")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_volume_buckets():
    buckets = ["3-5", "8-12", "15-20", "20-30", "35-40", "82", "90", "120"]
    rows = [[InlineKeyboardButton(text=f"до {b} м³", callback_data=f"vb:{b}")] for b in buckets]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="back:truck")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ftl_ltl():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отдельная машина (FTL)", callback_data="ftl:ftl")],
        [InlineKeyboardButton(text="Догруз (LTL)", callback_data="ftl:ltl")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:vol")],
    ])

def kb_loading():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Боковая", callback_data="ld:side"),
         InlineKeyboardButton(text="Задняя", callback_data="ld:rear"),
         InlineKeyboardButton(text="Верхняя", callback_data="ld:top")],
        [InlineKeyboardButton(text="Не знаю", callback_data="ld:unknown")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:route_to")],
    ])

def kb_review():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="review:confirm")],
        [InlineKeyboardButton(text="Изменить", callback_data="review:edit")],
        [InlineKeyboardButton(text="« Назад", callback_data="back:loading")],
    ])

def kb_rate_result():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подключить логиста", callback_data="rate:need_logistic")],
        [InlineKeyboardButton(text="Оформить заявку", callback_data="rate:create_order")],
        [InlineKeyboardButton(text="Главное меню", callback_data="back:menu")],
    ])



# ===================== Заглушка расчёта ставки (до ATI) =====================

async def simple_rate_fallback(draft: QuoteDraft) -> int:
    """
    ВРЕМЕННО: простая заглушка, пока не подключён ATI.

    Здесь будет новый pipeline:
    - GPT нормализует заявку (города, кузов, тоннаж)
    - ATI возвращает рыночные ставки
    - GPT красиво оформляет ответ

    Сейчас просто возвращаем фиксированную цифру.
    """
    return 50000

# ===================== Обработчики =====================

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await clean_tmp(m.from_user.id)

    photo = FSInputFile("/app/app/images/1.png")
    await m.answer_photo(photo)

    text = (
        "Привет! На связи Aéza Logistic.\n"
        "Этот бот за 2 минуты посчитает стоимость перевозки из точки А в точку Б 🚛"
    )
    await send_tmp(m, text, reply_markup=kb_main())



# Режим «просто вопрос»

@router.callback_query(F.data == "mode:ask")
async def mode_ask(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.JUST_ASK_INPUT)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(
        cq.from_user.id,
        "Напишите ваш вопрос ✍️",
        reply_markup=kb_ask_question(),
    )


@router.message(Flow.JUST_ASK_INPUT, F.text.len() > 0)
async def just_ask_input(m: Message, state: FSMContext):
    # 1) отправляем запрос во внутренний API (как раньше)
    payload = {
        "tg_id": str(m.from_user.id),
        "name": m.from_user.full_name,
        "topic": "question",
        "text": m.text,
    }
    async with aiohttp.ClientSession() as s:
        try:
            await s.post(f"{API_BASE}/v1/tickets", json=payload, timeout=10)
        except Exception:
            # не ломаем UX, просто молча продолжаем
            pass

        inbox_tid = await _get_inbox_thread_id()
    text = (
        "❓ Новый вопрос от клиента\n\n"
        f"Клиент: {m.from_user.full_name} • TG ID {m.from_user.id}\n\n"
        f"Вопрос:\n{m.text}"
    )

    # 🔹 та же кнопка, что и в просчётах — используем callback take:calc:<tg_id>
    kb_inbox = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Взять клиента",
                callback_data=f"take:calc:{m.from_user.id}",
            )]
        ]
    )

    try:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            text=text,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except TelegramMigrateToChat as e:
        await bot.send_message(
            chat_id=e.migrate_to_chat_id,
            text=text,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except Exception:
        # тоже не роняем обработчик
        pass

    # 3) чистим временные сообщения и состояние
    await clean_tmp(m.from_user.id)
    await state.clear()

    # 4) отвечаем клиенту
    await m.answer("Принято ✅ К вам подключается менеджер")

@router.callback_query(F.data == "mode:call")
async def mode_call(cq: CallbackQuery, state: FSMContext):
    # переходим в сценарий "запрос звонка"
    await state.set_state(CallFlow.CALLBACK_PHONE)
    await clean_tmp(cq.from_user.id)

    await send_tmp_by_id(
        cq.from_user.id,
        "Напишите номер телефона, мы вам перезвоним ☎️",
        reply_markup=kb_step_main(),  # кнопка "Главное меню"
    )

    await cq.answer()

@router.message(CallFlow.CALLBACK_PHONE, F.text.len() > 0)
async def callback_phone(m: Message, state: FSMContext):
    phone = m.text.strip()

    # 1) Шлём тикет во внутренний API
    payload = {
        "tg_id": str(m.from_user.id),
        "name": m.from_user.full_name,
        "topic": "callback",
        "text": phone,
    }
    async with aiohttp.ClientSession() as s:
        try:
            await s.post(f"{API_BASE}/v1/tickets", json=payload, timeout=10)
        except Exception:
            # не ломаем UX, если API недоступно
            pass

    # 2) Дублируем запрос звонка в чат менеджеров
    inbox_tid = await _get_inbox_thread_id()
    text = (
        "📞 Запрос звонка от клиента\n\n"
        f"Клиент: {m.from_user.full_name} • TG ID {m.from_user.id}\n"
        f"Телефон: {phone}"
    )

    kb_inbox = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Взять клиента",
                callback_data=f"take:calc:{m.from_user.id}",
            )]
        ]
    )

    try:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            text=text,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except TelegramMigrateToChat as e:
        await bot.send_message(
            chat_id=e.migrate_to_chat_id,
            text=text,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except Exception:
        # тоже не роняем обработчик
        pass

    # 3) Чистим временные сообщения и состояние
    await clean_tmp(m.from_user.id)
    await state.clear()

    # 4) Ответ клиенту
    await m.answer("Спасибо! Мы вам перезвоним ✅")

# Режим «просчёт»

@router.callback_query(F.data == "mode:calc")
async def mode_calc(cq: CallbackQuery, state: FSMContext):
    await state.update_data(draft=asdict(QuoteDraft()))
    await state.set_state(Flow.CARGO_FORMAT)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какого формата ваш груз?", reply_markup=kb_format())
    await cq.answer()

@router.callback_query(F.data == "back:menu")
async def back_menu(cq: CallbackQuery, state: FSMContext):
    # очищаем состояние и временные сообщения
    await state.clear()
    await clean_tmp(cq.from_user.id)

    text = (
        "Привет! На связи Aéza Logistic.\n"
        "Этот бот быстро посчитает стоимость перевозки груза из точки А в точку Б 🚛"
    )

    # показываем тот же самый стартовый экран, что и в /start
    await send_tmp_by_id(
        cq.from_user.id,
        text,
        reply_markup=kb_main(),
    )

    await cq.answer()




# Выбор формата

@router.callback_query(F.data.startswith("fmt:"), Flow.CARGO_FORMAT)
async def choose_format(cq: CallbackQuery, state: FSMContext):
    fmt = cq.data.split(":")[1]
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.cargo_format = fmt
    await state.update_data(draft=asdict(draft))

    if fmt == "container":
        await state.set_state(Flow.CONTAINER_TYPE)
        await clean_tmp(cq.from_user.id)
        await send_tmp_by_id(cq.from_user.id, "Какой контейнер?", reply_markup=kb_container_type())
    elif fmt == "general":
        await state.set_state(Flow.PALLETS_WEIGHT)
        await clean_tmp(cq.from_user.id)
        await send_tmp_by_id(cq.from_user.id, "Какая нужна машина по грузоподъёмности?", reply_markup=kb_truck())
    else:
        await state.set_state(Flow.OVERSIZE_DIMENSIONS)
        await clean_tmp(cq.from_user.id)
        await send_tmp_by_id(cq.from_user.id, "Укажите габариты (Д×Ш×В, м) и вес (кг/т).")
    await cq.answer()

@router.callback_query(F.data == "back:fmt")
async def back_to_fmt(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.CARGO_FORMAT)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какого формата ваш груз?", reply_markup=kb_format())
    await cq.answer()

# Container

@router.callback_query(F.data.startswith("ct:"), Flow.CONTAINER_TYPE)
async def container_type(cq: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.container_hc = (cq.data == "ct:hc")
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.CONTAINER_VOLUME)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какой объём (ft)?", reply_markup=kb_container_volume())
    await cq.answer()

@router.callback_query(F.data == "back:ctype", Flow.CONTAINER_VOLUME)
async def back_ctype(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.CONTAINER_TYPE)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какой контейнер?", reply_markup=kb_container_type())
    await cq.answer()

@router.callback_query(F.data.startswith("cv:"), Flow.CONTAINER_VOLUME)
async def container_volume(cq: CallbackQuery, state: FSMContext):
    vol = int(cq.data.split(":")[1])
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.container_volume = vol
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.CONTAINER_RETURN)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(
        cq.from_user.id,
        "Контейнер остаётся у получателя или возвращаем пустым?",
        reply_markup=kb_container_return(),
    )
    await cq.answer()

@router.callback_query(F.data == "back:cvol", Flow.CONTAINER_RETURN)
async def back_cvol(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.CONTAINER_VOLUME)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какой объём (ft)?", reply_markup=kb_container_volume())
    await cq.answer()

@router.callback_query(F.data.startswith("cr:"), Flow.CONTAINER_RETURN)
async def container_return(cq: CallbackQuery, state: FSMContext):
    keep = (cq.data == "cr:keep")
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.container_return_empty = (not keep)
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.ROUTE_FROM)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Откуда забираем груз? Напишите город.", reply_markup=kb_back("ctype"))
    await cq.answer()

# General (LTL/FTL)

@router.callback_query(F.data.startswith("truck:"), Flow.PALLETS_WEIGHT)
async def pallets_weight(cq: CallbackQuery, state: FSMContext):
    cls = cq.data.split(":")[1]
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.truck_class = cls
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.PALLETS_VOLUME)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какой объём м³?", reply_markup=kb_volume_buckets())
    await cq.answer()

@router.callback_query(F.data == "back:truck", Flow.PALLETS_VOLUME)
async def back_truck(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.PALLETS_WEIGHT)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какая нужна машина по грузоподъёмности?", reply_markup=kb_truck())
    await cq.answer()

@router.callback_query(F.data.startswith("vb:"), Flow.PALLETS_VOLUME)
async def pallets_volume(cq: CallbackQuery, state: FSMContext):
    bucket = cq.data.split(":")[1]
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.volume_bucket = bucket
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.FTL_LTL)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Отдельная машина или догруз?", reply_markup=kb_ftl_ltl())
    await cq.answer()

@router.callback_query(F.data == "back:vol", Flow.FTL_LTL)
async def back_vol(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.PALLETS_VOLUME)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Какой объём м³?", reply_markup=kb_volume_buckets())
    await cq.answer()

@router.callback_query(F.data.startswith("ftl:"), Flow.FTL_LTL)
async def set_ftl_ltl(cq: CallbackQuery, state: FSMContext):
    mode = cq.data.split(":")[1]  # ftl | ltl
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.ftl_ltl = mode
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.ROUTE_FROM)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Откуда забираем груз? Напишите город.", reply_markup=kb_back("vol"))
    await cq.answer()

# Oversize

DIM_RE = re.compile(r"(?P<L>\d+(?:[.,]\d+)?)\D+(?P<W>\d+(?:[.,]\d+)?)\D+(?P<H>\d+(?:[.,]\d+)?)", re.IGNORECASE)
WEIGHT_RE = re.compile(r"(?P<W>\d+(?:[.,]\d+)?)\s*(?:кг|т|kg|ton|tons)?", re.IGNORECASE)

@router.message(Flow.OVERSIZE_DIMENSIONS)
async def oversize_dims(m: Message, state: FSMContext):
    text = m.text or ""
    dims = DIM_RE.search(text)
    wmatch = WEIGHT_RE.search(text)
    if not dims or not wmatch:
        await clean_tmp(m.from_user.id)
        await send_tmp(m, "Укажите габариты (Д×Ш×В, м) и вес (кг/т). Например: 6.5x2.4x3.1, 8.5т")
        return

    L = float(dims.group("L").replace(",", "."))
    W = float(dims.group("W").replace(",", "."))
    H = float(dims.group("H").replace(",", "."))
    weight_raw = wmatch.group("W").replace(",", ".")
    weight = float(weight_raw)
    if "т" in text.lower() or "ton" in text.lower():
        weight *= 1000.0
    weight = int(round(weight))

    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.length_m, draft.width_m, draft.height_m, draft.weight_kg = L, W, H, weight
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.ROUTE_FROM)
    await clean_tmp(m.from_user.id)
    await send_tmp(m, "Откуда забираем груз? Напишите город.", reply_markup=kb_back("fmt"))

# Маршрут и погрузка

@router.message(Flow.ROUTE_FROM, F.text.len() > 0)
async def route_from(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.route_from = m.text.strip()
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.ROUTE_TO)
    await clean_tmp(m.from_user.id)
    await send_tmp(m, "Куда везём? Напишите город назначения.", reply_markup=kb_back("route_from"))

@router.message(Flow.ROUTE_TO, F.text.len() > 0)
async def route_to(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.route_to = m.text.strip()
    await state.update_data(draft=asdict(draft))

    await state.set_state(Flow.LOADING_TYPE)
    await clean_tmp(m.from_user.id)
    await send_tmp(m, "Тип погрузки/разгрузки?", reply_markup=kb_loading())

@router.callback_query(F.data.startswith("ld:"), Flow.LOADING_TYPE)
async def loading_type(cq: CallbackQuery, state: FSMContext):
    typ = cq.data.split(":")[1]  # side|rear|top|unknown
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.loading = typ
    await state.update_data(draft=asdict(draft))

    # Единый красивый ПРЕДПРОСМОТР (та же карточка, ставка ещё не посчитана)
    await state.set_state(Flow.REVIEW)
    await clean_tmp(cq.from_user.id)
    d = QuoteDraft(**(await state.get_data())["draft"])
    preview = render_application(d, rate_rub=None)  # «—» в поле ставки
    await send_tmp_by_id(cq.from_user.id, preview, reply_markup=kb_review())
    await cq.answer()

@router.callback_query(F.data == "review:edit", Flow.REVIEW)
async def review_edit(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.CARGO_FORMAT)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(cq.from_user.id, "Что поправим? Начнём с формата груза:", reply_markup=kb_format())
    await cq.answer()

# Подтверждение и расчёт

@router.callback_query(F.data == "review:confirm", Flow.REVIEW)
async def review_confirm(cq: CallbackQuery, state: FSMContext):

    await state.set_state(Flow.RATE)

    # временное сообщение «считаем»
    calc_msg = await send_tmp_by_id(
        cq.from_user.id,
        "Считаем ставку по вашей заявке…"
    )

    data = await state.get_data()
    d = QuoteDraft(**data["draft"])

    # ==============================
    # 1) Пробуем полный ATI pipeline
    # ==============================
    ati_result = await ati_full_pipeline_simple(d)

    if ati_result and ati_result.get("rates"):
        # есть ставки по нескольким кузовам → красивый текст
        txt = await gpt_render_final_rate_simple(
            draft=d,
            rates=ati_result["rates"],
            user=cq.from_user,
        )
        rate_for_state = None
    else:
        # ==============================
        # 2) Фолбэк: одна цифра
        # ==============================
        rate = await gpt_estimate_rate(d)
        if rate is None:
            rate = 50000

        txt = render_simple_calc_application(
            d,
            rate,
            user_name=cq.from_user.full_name,
            user_id=cq.from_user.id,
        )
        rate_for_state = rate

    # удаляем сообщение «считаем»
    try:
        await bot.delete_message(
            chat_id=cq.from_user.id,
            message_id=calc_msg.message_id
        )
    except Exception:
        pass

    # чистим временные сообщения
    await clean_tmp(cq.from_user.id)

    # ==============================
    # 3) Клиенту
    # ==============================
    await bot.send_message(
        cq.from_user.id,
        txt,
        reply_markup=kb_rate_result(),
    )

    # ==============================
    # 4) Менеджерам — ТО ЖЕ САМОЕ
    # ==============================
    inbox_tid = await _get_inbox_thread_id()
    kb_inbox = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Взять клиента",
                    callback_data=f"take:calc:{cq.from_user.id}"
                )
            ]
        ]
    )

    card = txt + "\n\nСтатус: был только просчёт"

    try:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            text=card,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except TelegramMigrateToChat as e:
        await bot.send_message(
            chat_id=e.migrate_to_chat_id,
            text=card,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )

    # сохраняем avg_rate ТОЛЬКО если была одна цифра
    if rate_for_state is not None:
        d.avg_rate = rate_for_state
        await state.update_data(draft=asdict(d))

    await cq.answer()


from openai import AsyncOpenAI

# создаём правильный клиент
oai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def gpt_prepare_ati_request(draft: QuoteDraft) -> Optional[dict]:
    """
    НОВАЯ версия под новую OpenAI SDK /responses.create
    """

    if not oai_client:
        return None

    system_prompt = """
Ты профессиональный логист компании Aéza Logistic.

Твоя задача — разобрать заявку клиента на грузоперевозку и преобразовать её
в формат, необходимый для API «Средние ставки ATI».

Анализируй ЗАЯВКУ полностью и выполни 3 задачи.

────────────────────────────────────────
1) НОРМАЛИЗАЦИЯ ГОРОДОВ
────────────────────────────────────────
Верни название города погрузки и выгрузки в чистом виде, строго в формате ATI:

— только название города, без районов, улиц, областей и страны
— официальное написание («Москва», «Ростов-на-Дону», «Уфа»)
— исправляй опечатки и разговорные формы («Питер» → «Санкт-Петербург»)
— НЕ склоняй («из Москвы» → «Москва»)
— НЕ используй сокращения («СПб» → «Санкт-Петербург»)
— оставь только город, ничего лишнего.

Поля:
"from_city": "<город>",
"to_city": "<город>"

────────────────────────────────────────
2) ВЫБОР ПОДХОДЯЩИХ ТИПОВ КУЗОВОВ ATI
────────────────────────────────────────
Тебе нужно определить ВСЕ возможные типы кузовов, подходящие для перевозки
данного груза с указанным весом и объёмом.

Используй ТОЛЬКО эти значения ATI:

- "ref"    — рефрижератор (если нужен температурный режим,например, для продуктов питания)
- "close"  — закрытый фургон / цельнометаллический (подходит для бытовой техники, мебели, коробок, холодильников, переезда)
- "open"   — открытый / бортовой / площадка (для негабарита, строительных материалов, металла)
- "tent"   — тентованный (универсальный для большинства грузов)
- "tral"   — трал / низкорамник (только для спецтехники, негабарита по высоте/ширине/массе)
- "docker" — контейнер (обычно для морских контейнеров или грузов на длинные расстояния)

Правила:

— Выбери НЕ один, а ВСЕ реально подходящие кузова.
— НЕ выбирай "ref", если нет указания на температурный режим.
— НЕ выбирай "tral", если груз обычный и не превышает габариты/массу.
— Если груз универсальный (строительные материалы, коробки, паллеты) → обычно подходят "tent" и "open", иногда "close".
— Если груз мебель, техника, переезд → "close" и "tent".
— Если контейнерная доставка → "docker".
— Если на Дальний Восток / международно / логистические цепочки → "tent" + "docker".

Поле:
"car_types": ["tent", "close"]

────────────────────────────────────────
3) ПРИВЕДЕНИЕ ВЕСА К ТОННАЖУ ATI
────────────────────────────────────────
ATI принимает тоннажи строго:

1.5, 3, 5, 10, 20

Округление:

— 8 тонн → 10  
— 12 тонн → 20  
— 17 тонн → 20  
— «до 1.5 т» → 1.5  
— если клиент не указал вес → выбери наиболее логичное значение, основываясь на типе груза.

Поле:
"tonnage": <число>

────────────────────────────────────────
ФОРМАТ ОТВЕТА
────────────────────────────────────────

Верни СТРОГО JSON БЕЗ какого-либо текста вне JSON:

{
  "from_city": "...",
  "to_city": "...",
  "car_types": ["...", "..."],
  "tonnage": ...,
  "comment": "краткое объяснение выбора кузовов"
}

Никаких пояснений до или после JSON.
Никаких «Вот ваш ответ», «Готово», markdown и т.п.
"""

    # формируем user_text как и раньше
    user_text = (
        f"Груз: {draft.cargo_text}\n"
        f"Адрес погрузки: {draft.route_from}\n"
        f"Адрес выгрузки: {draft.route_to}\n"
        f"Вес: {draft.weight_text}\n"
        f"Объём: {draft.volume_text}\n"
        f"Режим перевозки: {'FTL' if draft.ftl_ltl == 'ftl' else 'LTL'}\n"
    )

    try:
        resp = await oai_client.responses.create(
            model=GPT_RATE_MODEL,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_output_tokens=300,
            temperature=0,
        )

        raw = resp.output_text.strip()
        data = json.loads(raw)

        # нормализация
        if "car_types" not in data or not isinstance(data["car_types"], list):
            data["car_types"] = ["tent"]

        # тоннаж
        try:
            data["tonnage"] = float(data.get("tonnage", 5.0))
        except Exception:
            data["tonnage"] = 5.0

        return data

    except Exception as e:
        log.warning("gpt_prepare_ati_request error: %s", e)
        return None

async def gpt_call(prompt: str) -> str:
    """
    Универсальный вызов GPT для текстовых ответов.
    Использует oai_client и модель из GPT_RATE_MODEL.
    """
    if not oai_client:
        # безопасный дефолт, если ключа нет
        return "Сейчас не удалось автоматически посчитать ставку, логист свяжется с вами для уточнения деталей."

    try:
        resp = await oai_client.responses.create(
            model=GPT_RATE_MODEL,
            input=[{"role": "user", "content": prompt}],
            max_output_tokens=800,
            temperature=0.2,
        )
        return resp.output_text.strip()
    except Exception as e:
        log.warning("gpt_call error: %s", e)
        return "Сейчас не удалось автоматически посчитать ставку, логист свяжется с вами для уточнения деталей."

async def gpt_render_final_rate_simple(draft: QuoteDraft, rates: list[dict], user) -> str:
    """
    Красивый текстовый блок ставок для ПРОСТОЙ линейки.
    На входе:
      - draft с полями cargo_text, route_from, route_to, weight_text, volume_text
      - rates — список dict’ов от ati_collect_full_rates:
        {"car_type": "tent", "with_nds": bool, "rate_from": ..., "rate_to": ...}
      - user — объект Telegram-пользователя
    """

    # Небольшой мэппинг кодов кузовов на человекочитаемые названия
    car_type_names = {
        "tent": "Тентованный",
        "close": "Фургон / цельнометалл",
        "open": "Открытый борт / площадка",
        "ref": "Рефрижератор",
        "tral": "Трал / низкорамник",
        "docker": "Контейнер",
    }

    # Готовим компактный JSON для GPT
    prepared_rates = []
    for r in rates:
        if not isinstance(r, dict):
            continue
        car = r.get("car_type")
        if not car:
            continue

        prepared_rates.append({
            "car_type": car,
            "car_type_human": car_type_names.get(car, car),
            "with_nds": bool(r.get("with_nds")),
            "rate_from": r.get("rate_from"),
            "rate_to": r.get("rate_to"),
        })

    payload_json = json.dumps(prepared_rates, ensure_ascii=False)

    cargo = draft.cargo_text or "-"
    route_from = draft.route_from or "-"
    route_to = draft.route_to or "-"
    weight = draft.weight_text or "-"
    volume = draft.volume_text or "-"

    prompt = f"""
Ты — профессиональный логист компании Aéza Logistic.
Сформируй аккуратный, структурированный ответ для клиента в Telegram на основе данных заявки и ставок ATI.

Данные заявки:
- Клиент: {user.full_name} • TG ID {user.id}
- Груз: {cargo}
- Адрес погрузки: {route_from}
- Адрес выгрузки: {route_to}
- Вес: {weight}
- Объём: {volume}

Ставки ATI (JSON, массив объектов):
{payload_json}

Требования к формату:
1. Сначала короткая строка-заголовок вида:
   "💰 Оценка рыночных ставок по вашей заявке:"

2. Дальше для каждого типа кузова отдельный блок такого вида:
   <Название кузова на русском>  
   без НДС: от XXX ₽  
   с НДС: от YYY ₽  

   Правила:
   - Если по кузову есть только варианты без НДС, выводи только строку "без НДС".
   - Если только с НДС — выводи только "с НДС".
   - Суммы форматируй с пробелами по тысячам (например, 53 240 ₽).
   - Если по кузову вообще нет ставки — этот кузов НЕ выводи.

3. В конце ОБЯЗАТЕЛЬНО добавь строку:
   "ℹ️ Это ориентировочная ставка. Для точного расчёта подключите логиста."

4. Никаких списков в виде JSON. Никаких технических пояснений. Только аккуратный читаемый текст для клиента.
"""

    text = await gpt_call(prompt)
    return text.strip()



# ===================== ATI: поиск CityId по названию города =====================

async def _ati_load_city_cache() -> None:
    """
    Один раз грузим all_directions и собираем кэш norm_city_name -> CityId.
    """
    global _ATI_CITY_CACHE_LOADED, _ATI_CITY_CACHE

    if _ATI_CITY_CACHE_LOADED:
        return
    if not ATI_API_TOKEN:
        logging.warning("ATI_API_TOKEN не задан, кэш городов не будет загружен")
        _ATI_CITY_CACHE_LOADED = True
        return

    params: dict[str, str] = {}
    if not ATI_USE_DEMO:
        params["demo"] = "false"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                ATI_ALL_DIRECTIONS_URL,
                params=params,
                headers={
                    "Authorization": f"Bearer {ATI_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            ) as r:
                if r.status != 200:
                    logging.warning("ATI all_directions status=%s", r.status)
                    _ATI_CITY_CACHE_LOADED = True
                    return

                data = await r.json()
    except Exception as e:
        logging.warning("ATI all_directions error: %s", e)
        _ATI_CITY_CACHE_LOADED = True
        return

    items = data.get("AllDirections") or data.get("allDirections") or []
    cache: dict[str, int] = {}

    for item in items:
        from_city_raw = (item.get("FromCity") or "").strip()
        to_city_raw = (item.get("ToCity") or "").strip()
        from_id = item.get("FromCityId")
        to_id = item.get("ToCityId")

        if from_city_raw and isinstance(from_id, int):
            norm = _normalize_city_for_ati(from_city_raw)
            if norm:
                cache.setdefault(norm, from_id)

        if to_city_raw and isinstance(to_id, int):
            norm = _normalize_city_for_ati(to_city_raw)
            if norm:
                cache.setdefault(norm, to_id)

    _ATI_CITY_CACHE = cache
    _ATI_CITY_CACHE_LOADED = True
    logging.info("ATI city cache loaded: %s cities", len(_ATI_CITY_CACHE))


def _normalize_city_for_ati(name: str) -> str:
    if not name:
        return ""
    n = name.strip().lower()

    mapping = {
        "спб": "санкт-петербург",
        "питер": "санкт-петербург",
        "санкт петербург": "санкт-петербург",
        "st petersburg": "санкт-петербург",
        "st. petersburg": "санкт-петербург",
        "мск": "москва",
        "г москва": "москва",
        "г. москва": "москва",
    }
    if n in mapping:
        return mapping[n]

    # "Москва, Россия" → "Москва"
    if "," in n:
        n = n.split(",", 1)[0].strip()

    # удаляем "город" / "г." / "г "
    for junk in ("город ", "г. ", "г "):
        if n.startswith(junk):
            n = n[len(junk):]

    return n

async def ati_resolve_city_id(name: str) -> Optional[int]:
    """
    Строгое сопоставление города с CityId.
    БЕЗ алиасов, БЕЗ fuzzy, БЕЗ регионов.
    """
    await _ati_load_city_cache()

    if not _ATI_CITY_CACHE:
        logging.warning("ATI: кэш городов пуст")
        return None

    norm = _normalize_city_for_ati(name)
    if not norm:
        return None

    city_id = _ATI_CITY_CACHE.get(norm)
    if city_id:
        return city_id

    logging.warning(
        "ATI: city not found in cache: name=%r norm=%r",
        name, norm
    )
    return None


from datetime import date, timedelta


def normalize_ati_tonnage(t: float) -> float:
    """
    ATI API средних ставок принимает только дискретные значения тоннажа:
    1.5, 3, 5, 10, 20 (см. документацию).
    Возвращаем ближайшее "вверх" (или 20).
    """
    try:
        t = float(str(t).replace(",", "."))
    except Exception:
        return 20.0
    if t <= 1.5:
        return 1.5
    if t <= 3:
        return 3.0
    if t <= 5:
        return 5.0
    if t <= 10:
        return 10.0
    return 20.0


from datetime import date, timedelta

# ===================== ATI: helpers (строго по документации) =====================

ATI_ALL_DIRECTIONS_V2_URL = "https://api.ati.su/priceline/license/v2/all_directions"
# ATI_AVERAGE_PRICES_URL уже объявлен выше в настройках


_ati_directions_cache: dict | None = None
_ati_directions_cache_loaded_at: float | None = None
_ATI_DIRECTIONS_TTL_SEC = 6 * 60 * 60  # 6 часов


async def _ati_http_json(
    method: str,
    url: str,
    *,
    json_payload: dict | None = None,
    params: dict | None = None,
    timeout: int = 25,
) -> tuple[int, dict | list | str]:
    """
    Унифицированный HTTP-вызов к ATI.
    Возвращает (status_code, parsed_json_or_text).
    """
    headers = {
        "Authorization": f"Bearer {ATI_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method.upper(),
                url,
                params=params,
                json=json_payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                status = resp.status
                # ATI иногда возвращает текст ошибки в JSON. Пытаемся распарсить, иначе текст.
                try:
                    data = await resp.json()
                except Exception:
                    data = await resp.text()
                return status, data
    except Exception as e:
        log.warning("ATI HTTP error %s %s: %s", method, url, e)
        return 0, {"error": "http_error", "reason": str(e)}


async def ati_load_all_directions_v2(force: bool = False) -> dict | None:
    """
    Кешируем список направлений (v2/all_directions), чтобы:
      - не слать лишние запросы,
      - валидировать сочетания тоннаж/кузов до запроса average_prices.

    Документация: /priceline/license/v2/all_directions.
    """
    global _ati_directions_cache, _ati_directions_cache_loaded_at

    now = time.time()
    if (
        not force
        and _ati_directions_cache is not None
        and _ati_directions_cache_loaded_at is not None
        and (now - _ati_directions_cache_loaded_at) < _ATI_DIRECTIONS_TTL_SEC
    ):
        return _ati_directions_cache

    if not ATI_API_TOKEN:
        log.warning("ATI_API_TOKEN не задан — нельзя загрузить all_directions")
        return None

    params = {"demo": "true"} if ATI_USE_DEMO else None
    status, data = await _ati_http_json("GET", ATI_ALL_DIRECTIONS_V2_URL, params=params)

    if status != 200 or not isinstance(data, dict):
        log.warning("ATI all_directions(v2) status=%s body=%r", status, data)
        return None

    _ati_directions_cache = data
    _ati_directions_cache_loaded_at = now
    return _ati_directions_cache


def _ati_get_available_cartypes_from_direction_info(direction_info: dict, tonnage: float) -> set[str]:
    """
    В v2/all_directions доступные сочетания лежат в:
      DirectionInfo.TonnageCartype  (тоннаж -> список кузовов)
    или
      DirectionInfo.CartypeTonnage  (кузов -> список тоннажей)

    Парсим максимально аккуратно.
    """
    t = normalize_ati_tonnage(tonnage)
    # В ответе ключи могут быть строками, например "10" или "10.0" или "1.5"
    t_keys = {str(int(t)) if float(t).is_integer() else str(t), str(t), str(int(t))}

    # 1) TonnageCartype
    tc = direction_info.get("TonnageCartype")
    if isinstance(tc, dict):
        for k in t_keys:
            v = tc.get(k)
            if isinstance(v, list):
                return {str(x) for x in v if x}

    # 2) CartypeTonnage
    ct = direction_info.get("CartypeTonnage")
    if isinstance(ct, dict):
        res = set()
        for car, tons in ct.items():
            if isinstance(tons, list):
                # tons могут быть строками/числами
                for x in tons:
                    sx = str(x)
                    if sx in t_keys:
                        res.add(str(car))
                        break
        return res

    return set()


async def ati_get_available_cartypes_for_direction(
    from_city_id: int,
    to_city_id: int,
    tonnage: float,
    *,
    round_trip: bool = False,
) -> set[str]:
    """
    Возвращает доступные ATI CarType для конкретного направления и тоннажа
    на основании v2/all_directions.

    Если не нашли направление — вернём пустое множество (и тогда pipeline сам решит fallback).
    """
    data = await ati_load_all_directions_v2()
    if not data or not isinstance(data, dict):
        return set()

    all_dirs = data.get("AllDirections")
    if not isinstance(all_dirs, list):
        return set()

    for d in all_dirs:
        if not isinstance(d, dict):
            continue
        if d.get("FromCityId") == from_city_id and d.get("ToCityId") == to_city_id:
            info_key = "RoundTripsInfo" if round_trip else "DirectionInfo"
            info = d.get(info_key) or {}
            if isinstance(info, dict):
                return _ati_get_available_cartypes_from_direction_info(info, tonnage)

    return set()


def _ati_normalize_cartype(car_type: str) -> str:
    """
    Приводим внутренние/человеческие названия к CarType ATI.

    Допустимые значения ATI (по документации):
      ref, close, open, tent, tral, docker
    """
    if not car_type:
        return "close"

    c = str(car_type).strip().lower()

    # частые синонимы
    mapping = {
        "closed": "close",
        "close": "close",
        "tent": "tent",
        "tented": "tent",
        "open": "open",
        "platform": "open",
        "ref": "ref",
        "refr": "ref",
        "refrigerator": "ref",
        "tral": "tral",
        "trawl": "tral",
        "docker": "docker",
        "container": "docker",
    }

    return mapping.get(c, c)


from datetime import date, timedelta
from typing import Optional

async def ati_fetch_rate_single(
    *,
    from_city_id: int,
    to_city_id: int,
    car_type: str,
    tonnage: float,
    with_nds: bool,
    days_back: int = 14,          # 👈 как на сайте
    round_trip: bool = False,
) -> Optional[dict]:
    """
    СТРОГО как считает сайт ATI.

    КЛЮЧЕВОЕ:
    - Frequency = "day"
    - DateFrom / DateTo
    - Берём ТОЛЬКО PricesInRub.AveragePrice
    - НИКАКИХ умножений
    """

    if not ATI_API_TOKEN:
        return None

    car = _ati_normalize_cartype(car_type)
    tonnage_value = normalize_ati_tonnage(tonnage)

    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=days_back)

    payload = {
        "From": {"CityId": from_city_id},
        "To": {"CityId": to_city_id},
        "CarType": car,
        "Tonnage": tonnage_value,
        "DateFrom": date_from.isoformat(),
        "DateTo": date_to.isoformat(),
        "Frequency": "day",          # 🔥 ВАЖНО
        "WithNds": bool(with_nds),
        "RoundTrip": bool(round_trip),
    }

    status, data = await _ati_http_json(
        "POST",
        ATI_AVERAGE_PRICES_URL,
        json_payload=payload,
        params=None,
    )

    if status != 200 or not isinstance(data, dict):
        return None

    items = data.get("Data")
    if not isinstance(items, list) or not items:
        return None

    item = items[-1]
    prices = item.get("PricesInRub")
    if not isinstance(prices, dict):
        return None

    avg = prices.get("AveragePrice")
    if not isinstance(avg, (int, float)):
        return None

    return {
        "car_type": car,
        "with_nds": with_nds,
        "tonnage": tonnage_value,
        "rate_from": int(round(avg)),
        "rate_to": int(round(prices.get("UpperPrice", avg))),
    }


async def ati_collect_full_rates(
    *,
    from_id: int,
    to_id: int,
    tonnage: float,
    car_types: list[str],
    with_nds: Optional[bool] = None,
) -> list[dict]:
    """
    Собирает ставки ATI:
    - 1 запрос = 1 кузов + 1 тоннаж + 1 НДС
    - НИКАКИХ вычислений внутри
    """

    results: list[dict] = []

    clean_car_types = [_ati_normalize_cartype(c) for c in car_types if c]
    seen = set()
    clean_car_types = [c for c in clean_car_types if not (c in seen or seen.add(c))]

    nds_options = (with_nds,) if with_nds is not None else (False, True)

    for car in clean_car_types:
        for nds in nds_options:
            item = await ati_fetch_rate_single(
                from_city_id=from_id,
                to_city_id=to_id,
                car_type=car,
                tonnage=tonnage,
                with_nds=nds,
            )

            if not item:
                log.warning(
                    "ATI: нет ставки (%s→%s) car=%s tonnage=%s nds=%s",
                    from_id, to_id, car, tonnage, nds
                )
                continue

            results.append(item)

    return results

async def ati_full_pipeline_simple(draft: QuoteDraft) -> Optional[dict]:
    """
    ЧИСТЫЙ ATI-Pipeline (строго по документации):
      1) GPT нормализует заявку (города + список кузовов + тоннаж)
      2) Резолвим города в CityId (по нашему кэшу)
      3) Нормализуем тоннаж в 1.5/3/5/10/20
      4) Берём доступные кузова для направления+тоннажа из v2/all_directions
      5) Делаем N запросов average_prices (по одному на кузов и НДС/без НДС)
      6) Если ставок нет — просим GPT подобрать логистические хабы и пробуем по ним
    """
    if not oai_client or not ATI_API_TOKEN:
        log.warning("ATI pipeline: нет OpenAI клиента или ATI токена")
        return None

    # 1) GPT → нормализация
    norm = await gpt_prepare_ati_request(draft)
    if not norm:
        log.warning("ATI pipeline: GPT вернул None")
        return None

    from_city = (norm.get("from_city") or draft.route_from or "").strip()
    to_city = (norm.get("to_city") or draft.route_to or "").strip()
    raw_car_types = norm.get("car_types") or []
    raw_tonnage = norm.get("tonnage")

    if not from_city or not to_city:
        log.warning("ATI pipeline: нет городов (%r → %r)", from_city, to_city)
        return None

    # 2) CityId
    from_id = await ati_resolve_city_id(from_city)
    to_id = await ati_resolve_city_id(to_city)
    if not from_id or not to_id:
        log.warning("ATI pipeline: не нашли CityId (%s → %s)", from_city, to_city)
        return None

    # 3) тоннаж
    if raw_tonnage is None:
        # fallback на draft.truck_class
        try:
            if draft.truck_class:
                raw_tonnage = float(str(draft.truck_class).replace(",", "."))
        except Exception:
            raw_tonnage = None

    tonnage = normalize_ati_tonnage(raw_tonnage or 20)

    # 4) кузова: сначала нормализуем, затем фильтруем через v2/all_directions
    requested = [_ati_normalize_cartype(x) for x in raw_car_types if x]
    if not requested:
        requested = ["tent", "close"]  # безопасный минимум

    available = await ati_get_available_cartypes_for_direction(from_id, to_id, tonnage, round_trip=False)
    if available:
        car_types = [c for c in requested if c in available]
        if not car_types:
            # если GPT попросил экзотику — берём 1-2 самых популярных из available
            prefer_local = ["tent", "close", "ref", "docker", "open", "tral"]
            car_types = [c for c in prefer_local if c in available][:2] or list(available)[:2]
    else:
        # если all_directions по какой-то причине не загрузился — просто валидируем на допустимые значения
        allowed = {"ref", "close", "open", "tent", "tral", "docker"}
        car_types = [c for c in requested if c in allowed]
        if not car_types:
            car_types = ["tent"]

    # 5) Запрос ставок (+ fallback стратегия с лимитом)
    MAX_TOTAL_REQUESTS = 24          # лимит на average_prices (кузов×НДС)
    MAX_CAR_TYPES = 4                # чтобы не раздувать число запросов
    prefer = ["tent", "close", "ref", "docker", "open", "tral"]

    def _pick_car_types(base: list[str], avail: set[str] | None) -> list[str]:
        """
        Выбираем до MAX_CAR_TYPES кузовов.
        Сначала пробуем base, затем popular prefer, затем просто первые из avail.
        """
        seen = set()
        base = [c for c in base if c and not (c in seen or seen.add(c))]
        if avail:
            primary = [c for c in base if c in avail]
            if primary:
                return primary[:MAX_CAR_TYPES]
            popular = [c for c in prefer if c in avail]
            return (popular[:MAX_CAR_TYPES] or list(avail)[:MAX_CAR_TYPES])
        # если avail нет — берём из base, иначе безопасный минимум
        return (base[:MAX_CAR_TYPES] or ["tent"])

    def _expected_requests(ctypes: list[str]) -> int:
        # ati_collect_full_rates по умолчанию ходит в (False, True) => *2
        return len(ctypes) * 2

async def ati_full_pipeline_simple(draft: QuoteDraft) -> Optional[dict]:
    """
    ЧИСТЫЙ ATI-Pipeline (строго по документации):
      1) GPT нормализует заявку (города + список кузовов + тоннаж)
      2) Резолвим города в CityId (по нашему кэшу)
      3) Нормализуем тоннаж в 1.5/3/5/10/20
      4) Берём доступные кузова для направления+тоннажа из v2/all_directions
      5) Делаем N запросов average_prices (по одному на кузов и НДС/без НДС)
      6) Если ставок нет — просим GPT подобрать хабы и повторяем по хабам (быстро, с лимитом)
    """
    if not oai_client or not ATI_API_TOKEN:
        log.warning("ATI pipeline: нет OpenAI клиента или ATI токена")
        return None

    # ----------------------------
# helper: GPT picks nearest hubs
# ----------------------------
async def _gpt_pick_hubs(from_name: str, to_name: str) -> Optional[dict]:
    """
    Возвращает JSON:
    {
      "from_hubs": ["Город1","Город2","Город3"],
      "to_hubs": ["Город1","Город2","Город3"],
      "why": "коротко почему",
      "confidence": 0.0
    }
    """
    client = oai_client
    if client is None:
        return None

    system = (
        "Ты — помощник логиста по России/СНГ. "
        "Нужно подобрать ближайшие логистические хабы для двух населённых пунктов, "
        "чтобы по этим хабам с высокой вероятностью была статистика ставок грузоперевозок.\n\n"
        "Верни СТРОГО валидный JSON без пояснений вокруг.\n"
        "Формат:\n"
        "{\n"
        '  "from_hubs": ["Город1","Город2","Город3"],\n'
        '  "to_hubs":   ["Город1","Город2","Город3"],\n'
        '  "why": "коротко почему эти хабы",\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        "Требования:\n"
        "- Только города (без области/района).\n"
        "- По 3 кандидата на каждую сторону.\n"
        "- Хабы должны быть крупнее/более транспортными узлами, чем исходные точки.\n"
        "- Если исходный город уже крупный хаб — допускается вернуть его первым в списке.\n"
        "- Не придумывай несуществующие города.\n"
    )
    user = f"Маршрут: {from_name} → {to_name}. Подбери хабы."

    try:
        resp = await client.responses.create(   # ✅ ВОТ ЭТО КРИТИЧНО
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
    except Exception as e:
        log.warning("GPT hubs: request failed: %r", e)
        return None

    txt = (getattr(resp, "output_text", None) or "").strip()
    if not txt:
        return None

    import json
    try:
        data = json.loads(txt)
    except Exception:
        log.warning("GPT hubs: cannot parse JSON: %r", txt[:500])
        return None

    if not isinstance(data, dict):
        return None

    fh = data.get("from_hubs") or []
    th = data.get("to_hubs") or []
    if not isinstance(fh, list) or not isinstance(th, list):
        return None

    def _clean_list(xs: list) -> list[str]:
        out: list[str] = []
        for x in xs:
            if not x:
                continue
            s = str(x).strip()
            if not s:
                continue
            if len(s) > 60:
                s = s[:60].strip()
            if s not in out:
                out.append(s)
            if len(out) >= 3:
                break
        return out

    data["from_hubs"] = _clean_list(fh)
    data["to_hubs"] = _clean_list(th)
    data["why"] = str(data.get("why") or "").strip()[:240]
    try:
        data["confidence"] = float(data.get("confidence") or 0.0)
    except Exception:
        data["confidence"] = 0.0

    if not data["from_hubs"] or not data["to_hubs"]:
        return None

    return data

    # ----------------------------
    # helper: run ATI attempts (your current logic) for given cities
    # ----------------------------
    async def _run_attempts_for_route(
        *,
        from_city_name: str,
        to_city_name: str,
        norm_payload: dict,
        raw_car_types_list: list,
        raw_tonnage_val,
        global_budget: int,
    ) -> tuple[Optional[dict], int, Optional[tuple]]:
        """
        Возвращает:
          (result_dict|None, budget_left, last_empty_tuple|None)
        last_empty_tuple = (from_id, to_id, tonnage, car_types, reason)
        """
        # 2) CityId
        from_id = await ati_resolve_city_id(from_city_name)
        to_id = await ati_resolve_city_id(to_city_name)
        if not from_id or not to_id:
            log.warning("ATI pipeline: не нашли CityId (%s → %s)", from_city_name, to_city_name)
            return None, global_budget, None

        # 3) тоннаж
        raw_tonnage = raw_tonnage_val
        if raw_tonnage is None:
            try:
                if draft.truck_class:
                    raw_tonnage = float(str(draft.truck_class).replace(",", "."))
            except Exception:
                raw_tonnage = None

        tonnage = normalize_ati_tonnage(raw_tonnage or 20)

        # 4) кузова
        requested = [_ati_normalize_cartype(x) for x in (raw_car_types_list or []) if x]
        if not requested:
            requested = ["tent", "close"]

        available = await ati_get_available_cartypes_for_direction(from_id, to_id, tonnage, round_trip=False)
        if available:
            car_types = [c for c in requested if c in available]
            if not car_types:
                prefer0 = ["tent", "close", "ref", "docker", "open", "tral"]
                car_types = [c for c in prefer0 if c in available][:2] or list(available)[:2]
        else:
            allowed = {"ref", "close", "open", "tent", "tral", "docker"}
            car_types = [c for c in requested if c in allowed]
            if not car_types:
                car_types = ["tent"]

        # 5) attempts with budget limit
        MAX_TOTAL_REQUESTS = 24          # лимит на average_prices (кузов×НДС)
        MAX_CAR_TYPES = 4               # чтобы не раздувать число запросов
        prefer = ["tent", "close", "ref", "docker", "open", "tral"]

        def _pick_car_types(base: list[str], avail: set[str] | None) -> list[str]:
            """
            Выбираем до MAX_CAR_TYPES кузовов.
            Сначала пробуем base, затем popular prefer, затем просто первые из avail.
            """
            seen = set()
            base2 = [c for c in base if c and not (c in seen or seen.add(c))]
            if avail:
                primary = [c for c in base2 if c in avail]
                if primary:
                    return primary[:MAX_CAR_TYPES]
                popular = [c for c in prefer if c in avail]
                return (popular[:MAX_CAR_TYPES] or list(avail)[:MAX_CAR_TYPES])
            return (base2[:MAX_CAR_TYPES] or ["tent"])

        def _expected_requests(ctypes: list[str]) -> int:
            return len(ctypes) * 2  # (False, True)

        attempts: list[dict] = []
        attempts.append({"tonnage": tonnage, "car_types": _pick_car_types(car_types, available if available else None), "reason": "primary"})

        fallback_car_types = _pick_car_types(["tent", "close", "open", "ref"], available if available else None)
        if fallback_car_types != attempts[0]["car_types"]:
            attempts.append({"tonnage": tonnage, "car_types": fallback_car_types, "reason": "cartype_fallback"})

        if float(tonnage) != 20.0:
            fb_tonnage = 20.0
            fb_avail = await ati_get_available_cartypes_for_direction(from_id, to_id, fb_tonnage, round_trip=False)
            fb_car_types = _pick_car_types(attempts[0]["car_types"], fb_avail if fb_avail else None)
            attempts.append({"tonnage": fb_tonnage, "car_types": fb_car_types, "reason": "tonnage_to_20"})

        # бюджеты: локальный и глобальный
        local_budget = min(MAX_TOTAL_REQUESTS, global_budget)
        last_empty = None

        for idx, a in enumerate(attempts, start=1):
            t = a["tonnage"]
            ct = a["car_types"]
            need = _expected_requests(ct)

            if need > local_budget:
                log.warning(
                    "ATI: skip attempt #%s (%s) because budget exceeded: need=%s left=%s",
                    idx, a["reason"], need, local_budget
                )
                continue

            log.info(
                "ATI TRY #%s (%s): %s→%s tonnage=%s car_types=%s (budget=%s)",
                idx, a["reason"], from_id, to_id, t, ct, local_budget
            )

            rates = await ati_collect_full_rates(
                from_id=from_id,
                to_id=to_id,
                tonnage=t,
                car_types=ct,
            )

            local_budget -= need
            global_budget -= need

            if rates:
                log.info(
                    "ATI OK #%s (%s): got=%s rates; used tonnage=%s car_types=%s",
                    idx, a["reason"], len(rates), t, ct
                )
                return {
                    "normalized": norm_payload,
                    "from_city": from_city_name,
                    "to_city": to_city_name,
                    "from_id": from_id,
                    "to_id": to_id,
                    "tonnage": t,
                    "available_car_types": sorted(list(available)) if available else None,
                    "used_car_types": ct,
                    "rates": rates,
                    "fallback_used": a["reason"] if a["reason"] != "primary" else None,
                }, global_budget, None

            last_empty = (from_id, to_id, t, ct, a["reason"])
            log.warning(
                "ATI EMPTY #%s (%s): %s→%s tonnage=%s car_types=%s (budget_left=%s)",
                idx, a["reason"], from_id, to_id, t, ct, local_budget
            )

        if last_empty:
            fid, tid, t, ct, why = last_empty
            log.warning(
                "ATI pipeline: no rates after attempts. last=(%s) %s→%s tonnage=%s car_types=%s",
                why, fid, tid, t, ct
            )

        return None, global_budget, last_empty

    # ----------------------------
    # 1) GPT → нормализация
    # ----------------------------
    norm = await gpt_prepare_ati_request(draft)
    if not norm:
        log.warning("ATI pipeline: GPT вернул None")
        return None

    from_city = (norm.get("from_city") or draft.route_from or "").strip()
    to_city = (norm.get("to_city") or draft.route_to or "").strip()
    raw_car_types = norm.get("car_types") or []
    raw_tonnage = norm.get("tonnage")

    if not from_city or not to_city:
        log.warning("ATI pipeline: нет городов (%r → %r)", from_city, to_city)
        return None

    # ----------------------------
    # First: try original route (fast)
    # ----------------------------
    GLOBAL_BUDGET = 24  # общий лимит запросов average_prices за весь пайплайн (оригинал + хабы)
    result, GLOBAL_BUDGET, last_empty = await _run_attempts_for_route(
        from_city_name=from_city,
        to_city_name=to_city,
        norm_payload=norm,
        raw_car_types_list=raw_car_types,
        raw_tonnage_val=raw_tonnage,
        global_budget=GLOBAL_BUDGET,
    )
    if result:
        return result

    # ----------------------------
    # If no rates: try hubs (limited)
    # ----------------------------
    # Чтобы не "долго бить" в пустое направление — делаем хабы сразу после пустого исходного маршрута.
    hubs = await _gpt_pick_hubs(from_city, to_city)
    if not hubs:
        log.warning("ATI hubs: GPT не дал хабы (%s → %s)", from_city, to_city)
        return None

    from_hubs: list[str] = hubs.get("from_hubs") or []
    to_hubs: list[str] = hubs.get("to_hubs") or []

    log.warning(
        "ATI HUBS: %s→%s | from_hubs=%s | to_hubs=%s | why=%s",
        from_city, to_city, from_hubs, to_hubs, hubs.get("why")
    )

    # Ограничим число комбинаций, чтобы не улететь в запросы
    # 3x3 = 9 маршрутов, но мы пробуем по порядку и остановимся при успехе/исчерпании бюджета.
    for fh in from_hubs[:3]:
        for th in to_hubs[:3]:
            if fh.strip().lower() == from_city.strip().lower() and th.strip().lower() == to_city.strip().lower():
                continue  # уже пробовали
            if GLOBAL_BUDGET <= 0:
                log.warning("ATI hubs: budget exhausted, stop")
                return None

            # можно слегка "приглушить" лимит на каждый хаб-маршрут,
            # но оставим общий GLOBAL_BUDGET как основной стоп-кран.
            r, GLOBAL_BUDGET, _ = await _run_attempts_for_route(
                from_city_name=fh,
                to_city_name=th,
                norm_payload={**norm, "from_city": fh, "to_city": th, "hubs": hubs},
                raw_car_types_list=raw_car_types,
                raw_tonnage_val=raw_tonnage,
                global_budget=GLOBAL_BUDGET,
            )
            if r:
                # помечаем, что использовали хабы
                r["hubs_used"] = {"from": fh, "to": th, "hubs_meta": hubs}
                r["fallback_used"] = (r.get("fallback_used") or "hubs")
                return r

    log.warning("ATI hubs: no rates for any hub route (%s→%s)", from_city, to_city)
    return None


async def estimate_rate_via_ati(draft: QuoteDraft) -> Optional[int]:
    """
    Резервная функция: вернуть одну цифру по ATI (минимум из найденных rate_from).
    Используется как fallback в простых местах.
    """
    res = await ati_full_pipeline_simple(draft)
    if not res or not res.get("rates"):
        return None

    numeric = [
        r.get("rate_from")
        for r in res["rates"]
        if isinstance(r, dict) and isinstance(r.get("rate_from"), (int, float))
    ]
    return int(min(numeric)) if numeric else None



async def gpt_estimate_rate(draft: QuoteDraft) -> Optional[int]:
    """
    Универсальный расчёт ставки «одной цифрой»:
    1) Пытаемся использовать полный простой ATI-pipeline (ati_full_pipeline_simple).
       Берём минимальную ставку «от» из всех ставок.
    2) Если не получилось — fallback через estimate_rate_via_ati (одна цифра ATI).
    3) Если и там нет ответа — ещё один fallback simple_rate_fallback.
    """
    # 1) Пробуем полноценный ATI simple-pipeline
    ati_result = await ati_full_pipeline_simple(draft)
    if ati_result and ati_result.get("rates"):
        numeric_rates = [
            r.get("rate_from")
            for r in ati_result["rates"]
            if isinstance(r, dict) and isinstance(r.get("rate_from"), (int, float))
        ]
        if numeric_rates:
            return int(min(numeric_rates))

    # 2) Fallback: старая логика «одной цифрой» через ATI
    rate = await estimate_rate_via_ati(draft)
    if rate is not None:
        return rate

    # 3) Жёсткий fallback, если совсем ничего не получилось
    return await simple_rate_fallback(draft)

# Новый режим «просчёт» (простая линейка)

@router.callback_query(F.data == "mode:calc_simple")
async def mode_calc_simple(cq: CallbackQuery, state: FSMContext):
    # создаём новый черновик заявки
    draft = QuoteDraft()
    draft.cargo_format = "general"  # всегда обычный груз
    draft.ftl_ltl = "ftl"           # по умолчанию отдельная машина
    await state.update_data(draft=asdict(draft))

    await ensure_quote_header(cq.from_user.id, state)

    await state.set_state(CalcFlow.CARGO)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(
        cq.from_user.id,
        "Что везем? Опишите коротко ваш груз (например, стройматериалы)",
        reply_markup=kb_step_main(),
    )
    await cq.answer()


@router.message(CalcFlow.CARGO, F.text.len() > 0)
async def calc_cargo(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))

    setattr(draft, "cargo_text", m.text.strip())
    await state.update_data(draft=asdict(draft))          # ← сначала сохраняем

    await ensure_quote_header(m.from_user.id, state)      # ← потом обновляем шапку

    await state.set_state(CalcFlow.FROM)
    await clean_tmp(m.from_user.id)
    await send_tmp(
        m,
        "Откуда везем? Напишите адрес погрузки:",
        reply_markup=kb_step_main(),
    )



@router.message(CalcFlow.FROM, F.text.len() > 0)
async def calc_from(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.route_from = m.text.strip()
    await state.update_data(draft=asdict(draft))

    await state.set_state(CalcFlow.TO)
    await clean_tmp(m.from_user.id)
    await ensure_quote_header(m.from_user.id, state)
    await send_tmp(
        m,
        "Куда везем? Напишите адрес выгрузки:",
        reply_markup=kb_step_main(),
    )


@router.message(CalcFlow.TO, F.text.len() > 0)
async def calc_to(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.route_to = m.text.strip()
    await state.update_data(draft=asdict(draft))          # ← сначала в state

    await ensure_quote_header(m.from_user.id, state)      # ← потом шапка

    await state.set_state(CalcFlow.WEIGHT)
    await clean_tmp(m.from_user.id)
    await send_tmp_photo(
         m,
         "/app/app/images/2.png",
    )

    await send_tmp_by_id(
         m.from_user.id,
         "Какой вес груза?",
         reply_markup=kb_weight_simple(),
     )

    



@router.callback_query(F.data.startswith("wgt:"), CalcFlow.WEIGHT)
async def calc_weight(cq: CallbackQuery, state: FSMContext):
    code = cq.data.split(":")[1]
    if code == "other":
        await state.set_state(CalcFlow.WEIGHT_CUSTOM)
        await clean_tmp(cq.from_user.id)
        await send_tmp_by_id(
            cq.from_user.id,
            "Укажите вес груза (в тоннах или кг), например: 8 т или 3200 кг",
            reply_markup=kb_step_main(),
        )
        return await cq.answer()

    tonnage = float(code)  # 1.5 / 3 / 5 / 10 / 20
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.truck_class = str(tonnage)
    setattr(draft, "weight_text", f"до {code} т")
    await state.update_data(draft=asdict(draft))

    # 🔹 обновляем шапку заявки
    await ensure_quote_header(cq.from_user.id, state)

    await state.set_state(CalcFlow.VOLUME)
    await clean_tmp(cq.from_user.id)

    await send_tmp_photo_by_user_id(
         cq.from_user.id,
         "/app/app/images/3.png",
    )     

    await send_tmp_by_id(
         cq.from_user.id,
         "Какой объём груза м³?",
         reply_markup=kb_volume_simple(),
    )



@router.message(CalcFlow.WEIGHT_CUSTOM, F.text.len() > 0)
async def calc_weight_custom(m: Message, state: FSMContext):
    text = m.text.strip()
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    setattr(draft, "weight_text", text)

    # попытка вытащить тонны для GPT (если получится)
    num = re.findall(r"\d+(?:[.,]\d+)?", text)
    if num:
        try:
            value = float(num[0].replace(",", "."))
            if "кг" in text.lower():
                value = value / 1000.0
            draft.truck_class = str(value)
        except Exception:
            pass

    await state.update_data(draft=asdict(draft))

    # 🔹 обновляем шапку заявки
    await ensure_quote_header(m.from_user.id, state)

    await state.set_state(CalcFlow.VOLUME)
    await clean_tmp(m.from_user.id)

    await send_tmp_photo(
         m,
         "/app/app/images/3.png",
    )

    await send_tmp(
         m,
         "Какой объём груза м³?",
         reply_markup=kb_volume_simple(),
    )




@router.callback_query(F.data.startswith("vol:"), CalcFlow.VOLUME)
async def calc_volume(cq: CallbackQuery, state: FSMContext):
    code = cq.data.split(":")[1]
    if code == "other":
        await state.set_state(CalcFlow.VOLUME_CUSTOM)
        await clean_tmp(cq.from_user.id)
        await send_tmp_by_id(
            cq.from_user.id,
            "Укажите объём груза в м³, например: 18",
            reply_markup=kb_step_main(),
        )
        return await cq.answer()

    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.volume_bucket = code  # например "20-30"
    setattr(draft, "volume_text", f"до {code} м³")
    await state.update_data(draft=asdict(draft))

        # 🔹 обновляем шапку заявки
    await ensure_quote_header(cq.from_user.id, state)

    await state.set_state(CalcFlow.FTL_MODE)
    await clean_tmp(cq.from_user.id)
    await send_tmp_by_id(
        cq.from_user.id,
        "Вам нужна отдельная машина (FTL) или можно догрузом (LTL)?",
        reply_markup=kb_ftl_ltl_simple(),
    )
    await cq.answer()


@router.message(CalcFlow.VOLUME_CUSTOM, F.text.len() > 0)
async def calc_volume_custom(m: Message, state: FSMContext):
    text = m.text.strip()
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    setattr(draft, "volume_text", text)

    num = re.findall(r"\d+(?:[.,]\d+)?", text)
    if num:
        try:
            draft.volume_bucket = num[0]
        except Exception:
            pass

    await state.update_data(draft=asdict(draft))

        # 🔹 обновляем шапку заявки
    await ensure_quote_header(m.from_user.id, state)

    await state.set_state(CalcFlow.FTL_MODE)
    await clean_tmp(m.from_user.id)
    await send_tmp(
        m,
        "Вам нужна отдельная машина (FTL) или можно догрузом (LTL)?",
        reply_markup=kb_ftl_ltl_simple(),
    )


@router.callback_query(F.data.startswith("sftl:"), CalcFlow.FTL_MODE)
async def calc_ftl_mode(cq: CallbackQuery, state: FSMContext):
    mode = cq.data.split(":")[1]  # 'ftl' или 'ltl'

    # обновляем драфт режимом FTL/LTL
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    draft.ftl_ltl = mode
    await state.update_data(draft=asdict(draft))

        # сразу пробуем удалить шапку, но ID не затираем —
    # если не получится, calc_confirm попробует ещё раз
    data = await state.get_data()
    header_id = data.get("quote_header_id")
    if header_id:
        try:
            await bot.delete_message(chat_id=cq.from_user.id, message_id=header_id)
        except Exception:
            # просто логируем, но quote_header_id сохраняем
            log.warning("Не удалось удалить шапку в calc_ftl_mode: %s", header_id)


    # переходим в состояние REVIEW и чистим временные сообщения
    await state.set_state(CalcFlow.REVIEW)
    await clean_tmp(cq.from_user.id)

    # собираем актуальный драфт и показываем ревью
    d = QuoteDraft(**(await state.get_data())["draft"])
    preview = render_simple_calc_application(
        d,
        rate_rub=None,
        user_name=cq.from_user.full_name,
        user_id=cq.from_user.id,
    )

    await send_tmp_by_id(
        cq.from_user.id,
        preview,
        reply_markup=kb_calc_review(),
    )
    await cq.answer()


@router.callback_query(F.data == "calc:edit", CalcFlow.REVIEW)
async def calc_edit(cq: CallbackQuery, state: FSMContext):
    # запоминаем id сообщения с ревью, чтобы потом его перерисовать
    await state.update_data(review_message_id=cq.message.message_id)

    # показываем меню, что именно хотим изменить
    await send_tmp_by_id(
        cq.from_user.id,
        "Что хотите изменить?",
        reply_markup=kb_calc_edit_menu(),
    )
    await cq.answer()

@router.callback_query(F.data.startswith("cedit:"), CalcFlow.REVIEW)
async def calc_choose_edit_field(cq: CallbackQuery, state: FSMContext):
    action = cq.data.split(":")[1]  # cargo / from / to / weight / volume / cancel

    if action == "cancel":
        # просто убираем меню "что хотите изменить" и остаёмся в режиме ревью
        await clean_tmp(cq.from_user.id)
        await cq.answer("Изменение отменено")
        return

    # сохраняем, какое поле редактируем
    await state.update_data(edit_field=action)
    await state.set_state(CalcFlow.EDIT_FIELD)

    if action == "cargo":
        q = "Что везём? Опишите коротко ваш груз (например, стройматериалы):"
    elif action == "from":
        q = "Откуда везём? Напишите новый адрес погрузки:"
    elif action == "to":
        q = "Куда везём? Напишите новый адрес выгрузки:"
    elif action == "weight":
        q = "Какой вес груза? Напишите новый вес (например: до 10 т или 8 т):"
    elif action == "volume":
        q = "Какой объём груза? Напишите новый объём (например: до 90 м³):"
    else:
        q = "Введите новое значение:"

    await send_tmp_by_id(
        cq.from_user.id,
        q,
    )
    await cq.answer()

@router.message(CalcFlow.EDIT_FIELD, F.text.len() > 0)
async def calc_edit_field_input(m: Message, state: FSMContext):
    data = await state.get_data()
    draft = QuoteDraft(**data.get("draft", {}))
    field = data.get("edit_field")
    value = m.text.strip()

    # Обновляем только одно поле в драфте
    if field == "cargo":
        draft.cargo_text = value
    elif field == "from":
        draft.route_from = value
    elif field == "to":
        draft.route_to = value
    elif field == "weight":
        draft.weight_text = value
    elif field == "volume":
        draft.volume_text = value

    await state.update_data(draft=asdict(draft), edit_field=None)

    
    await state.update_data(draft=asdict(draft), edit_field=None)

    # 🔹 на этапе ревью шапка не нужна — удаляем, если вдруг есть
    data = await state.get_data()
    header_id = data.get("quote_header_id")
    if header_id:
        try:
            await bot.delete_message(chat_id=m.from_user.id, message_id=header_id)
        except Exception:
            pass
        await state.update_data(quote_header_id=None)

    # id старого ревью-сообщения
    review_msg_id = data.get("review_message_id")

    # удаляем временные сообщения: 
    # (вопрос «что изменить?», вопрос поля, ответ пользователя)
    await clean_tmp(m.from_user.id)

    # удаляем старое превью
    if review_msg_id:
        try:
            await bot.delete_message(chat_id=m.from_user.id, message_id=review_msg_id)
        except Exception:
            pass

    # формируем новое превью
    d = QuoteDraft(**(await state.get_data())["draft"])
    preview = render_simple_calc_application(
        d,
        rate_rub=None,
        user_name=m.from_user.full_name,
        user_id=m.from_user.id,
    )

    msg = await bot.send_message(
        m.from_user.id,
        preview,
        reply_markup=kb_calc_review(),
    )

    # сохраняем id нового превью
    await state.update_data(review_message_id=msg.message_id)

    # возвращаемся в состояние REVIEW
    await state.set_state(CalcFlow.REVIEW)

async def gpt_format_final_quote_request(request_text: str, ati_rates: list) -> str:
    """
    Отправляет GPT заявку + результаты ATI и получает красивый текст для клиента.
    """
    prompt = f"""
Ты — логист компании Aéza Logistic. 
Сгенерируй красивый и понятный расчёт ставки на основе данных ниже.

────────────────────────
ЗАЯВКА КЛИЕНТА:
{request_text}

────────────────────────
СТАВКИ ATI:
{json.dumps(ati_rates, ensure_ascii=False, indent=2)}

Сделай:
— разнеси результаты по каждому типу кузова
— укажи "с НДС" и "без НДС"
— добавь +10% сверху (это наша внутренняя корректировка)
— округли до десятков рублей
— оформи красиво, как в примере:

Тент  
• без НДС: от 46 750 ₽  
• с НДС: от 53 240 ₽  

ℹ️ Это ориентировочная ставка. Точный расчёт сделает логист.

Верни только текст ответа клиенту.
"""

    completion = await client.responses.create(
        model="gpt-4.1",
        input=prompt,
    )
    return completion.output_text

@router.callback_query(F.data == "calc:confirm", CalcFlow.REVIEW)
async def calc_confirm(cq: CallbackQuery, state: FSMContext):

    # --- 0) Сразу подтверждаем callback ---
    try:
        await cq.answer()
    except Exception as e:
        log.warning("Не удалось ответить на callback calc:confirm: %s", e)

    data = await state.get_data()

    # --- 1) Удаляем шапку ---
    header_id = data.get("quote_header_id")
    if header_id:
        try:
            await bot.delete_message(chat_id=cq.from_user.id, message_id=header_id)
        except Exception as e:
            log.warning("Не удалось удалить шапку: %s", e)
        else:
            await state.update_data(quote_header_id=None)

    # --- 2) Удаляем карточку-предпросмотр ---
    try:
        await bot.delete_message(
            chat_id=cq.from_user.id,
            message_id=cq.message.message_id,
        )
    except Exception:
        pass

    # --- 3) Чистим tmp ---
    await clean_tmp(cq.from_user.id)

    # --- 4) CALCULATING ---
    await state.set_state(CalcFlow.CALCULATING)

    # --- 5) Сообщение “минутку…” ---
    calc_msg = await bot.send_message(
        cq.from_user.id,
        "⏳ Считаем ставку, минутку...",
    )

    # Загружаем draft
    d = QuoteDraft(**data["draft"])

    # =====================================================================
    # 6) GPT → подготовка параметров ATI (ГЛАВНЫЙ БЛОК)
    # =====================================================================
    ati_prep = await gpt_prepare_ati_request(d)

    if ati_prep:
        if ati_prep.get("from_city"):
            d.route_from = ati_prep["from_city"]
        if ati_prep.get("to_city"):
            d.route_to = ati_prep["to_city"]

        d.car_types = ati_prep.get("car_types", ["tent"])
        d.tonnage = ati_prep.get("tonnage", 5.0)
        d.with_nds = [True, False]

        await state.update_data(draft=asdict(d))
    else:
        d.car_types = ["tent"]
        d.tonnage = 5.0
        d.with_nds = [True, False]

    # =====================================================================
    # 7) ATI PIPELINE
    # =====================================================================
    log.warning("DEBUG GPT → ATI Draft: %s", d)
    log.warning("CAR TYPES FOR ATI: %s", d.car_types)

    ati_result = await ati_full_pipeline_simple(d)
    approx_rate_for_crm: Optional[int] = None

    if not ati_result or not ati_result.get("rates"):
        # --- Fallback ---
        fallback_rate = await simple_rate_fallback(d)
        approx_rate_for_crm = fallback_rate

        client_text = render_simple_calc_application(
            d,
            fallback_rate,
            user_name=cq.from_user.full_name,
            user_id=cq.from_user.id,
        )

    else:
        rates = ati_result["rates"]

        # минимальная ставка для CRM
        numeric_rates = [
            r["rate_from"]
            for r in rates
            if isinstance(r, dict) and isinstance(r.get("rate_from"), (int, float))
        ]
        if numeric_rates:
            approx_rate_for_crm = min(numeric_rates)

        # шаблон без ставки
        header_text = render_simple_calc_application(
            d,
            rate_rub=None,
            user_name=cq.from_user.full_name,
            user_id=cq.from_user.id,
        )

        # GPT оформляет красивую таблицу
        try:
            rates_text = await gpt_render_final_rate_simple(d, rates, cq.from_user)
        except Exception:
            rates_text = "Не удалось красиво оформить ставки."

        client_text = header_text + "\n\n" + rates_text

    # =====================================================================
    # 8) Сохраняем avg_rate
    # =====================================================================
    if approx_rate_for_crm is not None:
        d.avg_rate = approx_rate_for_crm
        await state.update_data(draft=asdict(d))

    # --- 9) Удаляем «минутку» ---
    try:
        await bot.delete_message(chat_id=cq.from_user.id, message_id=calc_msg.message_id)
    except Exception:
        pass

    # --- 10) Отправляем клиенту ---
    await bot.send_message(
        cq.from_user.id,
        client_text,
        reply_markup=kb_rate_result(),
    )

    # 📸 10.1) Финальная картинка
    await send_tmp_photo(
         cq.message,
         "/app/app/images/4.png",
    )

    # --- 11) Отправляем менеджерам ---
    inbox_tid = await _get_inbox_thread_id()
    kb_inbox = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Взять клиента", callback_data=f"take:calc:{cq.from_user.id}")]
        ]
    )

    card = client_text + "\n\nСтатус: был только просчёт"

    try:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            text=card,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )
    except TelegramMigrateToChat as e:
        await bot.send_message(
            chat_id=e.migrate_to_chat_id,
            text=card,
            reply_markup=kb_inbox,
            message_thread_id=inbox_tid,
        )

    # --- 12) Переход к RATE ---
    await state.set_state(Flow.RATE)


# Дальнейшие действия

@router.callback_query(F.data.in_({"rate:need_logistic", "rate:create_order", "rate:menu"}), Flow.RATE)
async def rate_decision(cq: CallbackQuery, state: FSMContext):
    choice = cq.data
    if choice == "rate:menu":
        await state.clear()
        await clean_tmp(cq.from_user.id)
        await cmd_start(
            Message.model_construct(
                chat=cq.message.chat,
                from_user=cq.from_user,
                message_id=cq.message.message_id,
                date=cq.message.date,
                text="/start",
            ),
            state,
        )
        return await cq.answer()

    data = await state.get_data()
    d = QuoteDraft(**data.get("draft", {}))
    d.intent = "need_logistic" if choice == "rate:need_logistic" else "create_order"
    await state.update_data(draft=asdict(d))

    if d.intent == "need_logistic":
        await send_tmp_by_id(cq.from_user.id, "В течение 10 минут к вам подключится наш логист ✅")
    else:
        await send_tmp_by_id(cq.from_user.id, "Отлично! Скоро к вам подключится наш менеджер и оформит заявку ✅")
    await cq.answer()

# Назначение тикета на менеджера: создаём тему и линкуем клиента в Redis
@router.callback_query(F.data.startswith("take:calc"))
async def cb_take(cq: CallbackQuery):
    try:
        parts = cq.data.split(":")
        client_id = int(parts[-1]) if parts and parts[-1].isdigit() else None
        if not client_id:
            return await cq.answer("Не смог понять ID клиента.", show_alert=True)

        # 1) Проверки доступа
        me_admin = await bot.get_chat_member(chat_id=MANAGER_GROUP_ID, user_id=(await bot.get_me()).id)
        if getattr(me_admin, "status", "") not in {"administrator", "creator"}:
            return await cq.answer("Бот не админ в группе менеджеров. Дай права.", show_alert=True)

        # 2) Пытаемся создать тему (нужны включённые «Темы» в группе)
        mgr_name = cq.from_user.full_name or "Менеджер"
        topic = await bot.create_forum_topic(chat_id=MANAGER_GROUP_ID, name=f"Ticket — {mgr_name}")
        topic_id = topic.message_thread_id

        # 3) Сохраняем связь тема ↔ клиент
        await redis.set(THREAD_TO_CLIENT.format(tid=topic_id), client_id)
        await redis.set(CLIENT_TO_THREAD.format(uid=client_id), topic_id)

        # 4) Обновляем карточку и даём инструкции менеджеру
        try:
            await cq.message.edit_text((cq.message.text or "") + f"\n👤 Взял: {mgr_name}")
        except Exception:
            pass

        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            message_thread_id=topic_id,
            text=(
                "Диалог по заявке открыт. Пишите в этой теме — клиент будет получать сообщения.\n"
                "Для завершения напишите /close"
            ),
        )
        await cq.answer("Тикет назначен вам")
    except TelegramBadRequest as e:
        # Частый кейс: темы выключены в группе
        log.exception("take failed (bad request): %s", e)
        await cq.answer("Не удалось создать тему. Включи «Темы» в настройках группы и выдай боту право Manage Topics.", show_alert=True)
    except TelegramForbiddenError as e:
        log.exception("take failed (forbidden): %s", e)
        await cq.answer("Бот не админ/нет права на управление темами.", show_alert=True)
    except Exception as e:
        log.exception("take failed: %s", e)
        await cq.answer("Ошибка при создании темы", show_alert=True)

# Пересылка ответов менеджеров клиенту по маппингу thread_id → client_id
@router.message(F.chat.type.in_({"supergroup", "group"}))
async def relay_from_manager(m: Message):
    if m.chat.id != MANAGER_GROUP_ID:
        return
    # нужно отвечать в теме (thread)
    tid = getattr(m, "message_thread_id", None)
    if not tid:
        return
    try:
        client_id_str = await redis.get(THREAD_TO_CLIENT.format(tid=tid))
        if not client_id_str:
            return
        client_id = int(client_id_str)
        # Текст/медиа
        if m.text:
            await bot.send_message(client_id, m.text, parse_mode="HTML")
        elif m.photo:
            await bot.send_photo(client_id, m.photo[-1].file_id, caption=m.caption or "", parse_mode="HTML")
        elif m.document:
            await bot.send_document(client_id, m.document.file_id, caption=m.caption or "", parse_mode="HTML")
        # (Если нужно — добавить пересылку фото/доков: get_file → download → send_document)
    except Exception as e:
        log.warning("Не удалось переслать клиенту из темы %s: %s", tid, e)

# ===================== Запуск =====================

async def main():
    me = await bot.get_me()
    logging.info(f"Bot OK: @{me.username} ({me.id})")

    if BOT_PUBLIC_URL:
        # webhook-режим
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/telegram/webhook")
        await bot.set_webhook(BOT_PUBLIC_URL)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=8001)
        await site.start()
        logging.info(f"Webhook mode. URL={BOT_PUBLIC_URL}")
        try:
            await asyncio.Event().wait()
        finally:
            await bot.session.close()
            await redis.aclose()
            await runner.cleanup()
    else:
        # polling-режим
        logging.info("Polling mode")
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
            await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())

