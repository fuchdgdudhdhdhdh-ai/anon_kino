# -*- coding: utf-8 -*-
"""
main.py
Telegram-бот кинотеатра «Супер 8» на aiogram 3.x с вебхуком (для деплоя на Render).
Весь контент импортируется из content.py — здесь только логика.
"""

import asyncio
import logging
import os
from typing import Optional

import aiocron
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import ClientSession, ClientTimeout, web

import content

# ============================================================
#   НАСТРОЙКИ
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("❌ Не задана переменная окружения BOT_TOKEN!")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else ""
PORT = int(os.getenv("PORT", 10000))
PING_CRON_EXPRESSION = "*/10 * * * *"  # каждые 10 минут

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ============================================================
#   FSM СОСТОЯНИЯ ПРОЦЕССА ПОКУПКИ БИЛЕТА
# ============================================================

class Booking(StatesGroup):
    choosing_movie = State()
    choosing_session = State()
    choosing_hall = State()
    choosing_row = State()
    choosing_seat = State()
    choosing_snacks = State()
    confirming = State()
    searching_requisites = State()
    waiting_paid_button = State()
    waiting_receipt = State()


# ============================================================
#   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ — КЛАВИАТУРЫ
# ============================================================

def kb_main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎞️ Сегодняшняя афиша", callback_data="menu:poster")
    builder.button(text="🎟️ Мои билеты", callback_data="menu:tickets")
    builder.button(text="❓ Поддержка", callback_data="menu:support")
    builder.button(text="ℹ️ О кинотеатре", callback_data="menu:about")
    builder.adjust(1)
    return builder.as_markup()


def kb_back(callback_data: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data=callback_data)
    return builder.as_markup()


def kb_poster() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for movie in content.MOVIES:
        builder.button(
            text=f"🎬 {movie['title']} ({movie['age']})",
            callback_data=f"movie:{movie['id']}",
        )
    builder.button(text="◀️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def kb_movie_card(movie: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, session in enumerate(movie["sessions"]):
        builder.button(
            text=f"🕒 {session['time']} — от {session['price']} ₽",
            callback_data=f"session:{movie['id']}:{idx}",
        )
    builder.button(text="◀️ Назад к афише", callback_data="menu:poster")
    builder.adjust(1)
    return builder.as_markup()


def kb_halls(movie_id: int, session_idx: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hall in content.HALLS:
        modifier = content.HALL_PRICE_MODIFIER.get(hall, 0)
        extra = f" (+{modifier} ₽)" if modifier else ""
        builder.button(text=f"🏛️ {hall}{extra}", callback_data=f"hall:{hall}")
    builder.button(text="◀️ Назад", callback_data=f"movie:{movie_id}")
    builder.adjust(1)
    return builder.as_markup()


def kb_rows(movie_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in range(1, content.ROWS_COUNT + 1):
        builder.button(text=f"Ряд {row}", callback_data=f"row:{row}")
    builder.button(text="◀️ Назад", callback_data=f"movie:{movie_id}")
    builder.adjust(3)
    return builder.as_markup()


def kb_seats(row: int, movie_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for seat in range(1, content.SEATS_PER_ROW + 1):
        builder.button(text=f"💺 {seat}", callback_data=f"seat:{seat}")
    builder.button(text="◀️ Назад к рядам", callback_data=f"movie:{movie_id}")
    builder.adjust(5)
    return builder.as_markup()


def kb_snacks(selected: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, snack in content.SNACKS.items():
        mark = "✅ " if key in selected else ""
        builder.button(
            text=f"{mark}{snack['title']} — {snack['price']} ₽",
            callback_data=f"snack:{key}",
        )
    builder.button(text="➡️ Продолжить", callback_data="snacks_done")
    builder.button(text="◀️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def kb_confirm() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить", callback_data="pay")
    builder.button(text="◀️ Отменить", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def kb_paid_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Я оплатил", callback_data="paid")
    builder.button(text="◀️ Отменить", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


# ============================================================
#   БЕЗОПАСНОЕ РЕДАКТИРОВАНИЕ СООБЩЕНИЙ
# ============================================================

async def safe_edit(message: Message, text: str, markup: Optional[InlineKeyboardMarkup] = None):
    """Редактирует сообщение, игнорируя ошибку 'message is not modified'."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning(f"Ошибка редактирования сообщения: {e}")


# ============================================================
#   ОБРАБОТЧИКИ: СТАРТ И ГЛАВНОЕ МЕНЮ
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(content.WELCOME_TEXT, reply_markup=kb_main_menu())


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, content.WELCOME_TEXT, kb_main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:poster")
async def cb_poster(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Booking.choosing_movie)
    text = "🎞️ <b>Афиша на сегодня</b>\n\nВыберите фильм 👇"
    await safe_edit(callback.message, text, kb_poster())
    await callback.answer()


@router.callback_query(F.data == "menu:tickets")
async def cb_tickets(callback: CallbackQuery):
    await safe_edit(callback.message, content.NO_TICKETS_TEXT, kb_back())
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery):
    await safe_edit(callback.message, content.SUPPORT_TEXT, kb_back())
    await callback.answer()


@router.callback_query(F.data == "menu:about")
async def cb_about(callback: CallbackQuery):
    await safe_edit(callback.message, content.ABOUT_TEXT, kb_back())
    await callback.answer()


# ============================================================
#   ОБРАБОТЧИКИ: КАРТОЧКА ФИЛЬМА
# ============================================================

@router.callback_query(F.data.startswith("movie:"))
async def cb_movie_card(callback: CallbackQuery, state: FSMContext):
    movie_id = int(callback.data.split(":")[1])
    movie = content.get_movie_by_id(movie_id)
    if not movie:
        await callback.answer("Фильм не найден 😔", show_alert=True)
        return

    await state.update_data(movie_id=movie_id)
    await state.set_state(Booking.choosing_session)

    text = (
        f"🎬 <b>{movie['title']}</b>\n\n"
        f"🌍 Страна: <i>{movie['country']}</i>\n"
        f"🔞 Возраст: <b>{movie['age']}</b>\n"
        f"🎭 Жанр: <i>{movie['genre']}</i>\n\n"
        "🕒 Выберите сеанс 👇"
    )
    await safe_edit(callback.message, text, kb_movie_card(movie))
    await callback.answer()


@router.callback_query(F.data.startswith("session:"))
async def cb_session(callback: CallbackQuery, state: FSMContext):
    _, movie_id_str, session_idx_str = callback.data.split(":")
    movie_id, session_idx = int(movie_id_str), int(session_idx_str)
    movie = content.get_movie_by_id(movie_id)
    if not movie:
        await callback.answer("Фильм не найден 😔", show_alert=True)
        return

    session = movie["sessions"][session_idx]
    await state.update_data(
        movie_id=movie_id,
        movie_title=movie["title"],
        session_time=session["time"],
        base_price=session["price"],
    )
    await state.set_state(Booking.choosing_hall)

    text = (
        f"🎬 <b>{movie['title']}</b>\n"
        f"🕒 Сеанс: <b>{session['time']}</b>\n\n"
        "🏛️ Выберите зал 👇"
    )
    await safe_edit(callback.message, text, kb_halls(movie_id, session_idx))
    await callback.answer()


@router.callback_query(F.data.startswith("hall:"))
async def cb_hall(callback: CallbackQuery, state: FSMContext):
    hall = callback.data.split(":", 1)[1]
    data = await state.get_data()
    movie_id = data.get("movie_id")

    await state.update_data(hall=hall)
    await state.set_state(Booking.choosing_row)

    text = (
        f"🎬 <b>{data.get('movie_title')}</b>\n"
        f"🏛️ Зал: <b>{hall}</b>\n\n"
        "🎫 Выберите ряд 👇"
    )
    await safe_edit(callback.message, text, kb_rows(movie_id))
    await callback.answer()


@router.callback_query(F.data.startswith("row:"))
async def cb_row(callback: CallbackQuery, state: FSMContext):
    row = int(callback.data.split(":")[1])
    data = await state.get_data()
    movie_id = data.get("movie_id")

    await state.update_data(row=row)
    await state.set_state(Booking.choosing_seat)

    text = (
        f"🎬 <b>{data.get('movie_title')}</b>\n"
        f"🏛️ Зал: <b>{data.get('hall')}</b>\n"
        f"🎫 Ряд: <b>{row}</b>\n\n"
        "💺 Выберите место 👇"
    )
    await safe_edit(callback.message, text, kb_seats(row, movie_id))
    await callback.answer()


@router.callback_query(F.data.startswith("seat:"))
async def cb_seat(callback: CallbackQuery, state: FSMContext):
    seat = int(callback.data.split(":")[1])
    data = await state.get_data()

    await state.update_data(seat=seat, snacks=[])
    await state.set_state(Booking.choosing_snacks)

    text = (
        f"🎬 <b>{data.get('movie_title')}</b>\n"
        f"🎫 Ряд {data.get('row')}, место {seat}\n\n"
        "🍿 <b>Хотите что-нибудь из бара?</b>\n"
        "Отметьте нужные позиции и нажмите «➡️ Продолжить»"
    )
    await safe_edit(callback.message, text, kb_snacks([]))
    await callback.answer()


@router.callback_query(F.data.startswith("snack:"))
async def cb_snack_toggle(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("snacks", []))

    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)

    await state.update_data(snacks=selected)

    text = (
        f"🎬 <b>{data.get('movie_title')}</b>\n"
        f"🎫 Ряд {data.get('row')}, место {data.get('seat')}\n\n"
        "🍿 <b>Хотите что-нибудь из бара?</b>\n"
        "Отметьте нужные позиции и нажмите «➡️ Продолжить»"
    )
    await safe_edit(callback.message, text, kb_snacks(selected))
    await callback.answer()


@router.callback_query(F.data == "snacks_done")
async def cb_snacks_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(Booking.confirming)

    hall = data.get("hall")
    base_price = data.get("base_price", 0)
    ticket_price = base_price + content.HALL_PRICE_MODIFIER.get(hall, 0)

    selected_snacks = data.get("snacks", [])
    snacks_total = sum(content.SNACKS[k]["price"] for k in selected_snacks)
    snacks_lines = "\n".join(
        f"  • {content.SNACKS[k]['title']} — {content.SNACKS[k]['price']} ₽" for k in selected_snacks
    ) or "  • Ничего не выбрано"

    total = ticket_price + snacks_total
    await state.update_data(total=total)

    text = (
        "🧾 <b>Подтверждение заказа</b>\n\n"
        f"🎬 Фильм: <b>{data.get('movie_title')}</b>\n"
        f"🕒 Сеанс: <b>{data.get('session_time')}</b>\n"
        f"🏛️ Зал: <b>{hall}</b>\n"
        f"🎫 Место: ряд {data.get('row')}, место {data.get('seat')}\n"
        f"🎟️ Билет: <b>{ticket_price} ₽</b>\n\n"
        f"🍽️ Закуски:\n{snacks_lines}\n\n"
        f"💰 <b>Итого к оплате: {total} ₽</b>"
    )
    await safe_edit(callback.message, text, kb_confirm())
    await callback.answer()


@router.callback_query(F.data == "pay")
async def cb_pay(callback: CallbackQuery, state: FSMContext):
    """Шаг 1: показываем 'поиск реквизитов', затем удаляем это сообщение и присылаем реквизиты."""
    await callback.answer()
    await state.set_state(Booking.searching_requisites)

    # Редактируем карточку подтверждения на сообщение о поиске реквизитов
    await safe_edit(callback.message, content.SEARCHING_REQUISITES_TEXT, None)

    # Ждём заданное время (по умолчанию 15 секунд), не блокируя других пользователей
    await asyncio.sleep(content.PAYMENT_SEARCH_DELAY_SECONDS)

    # Удаляем сообщение о поиске реквизитов
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        logger.warning(f"Не удалось удалить сообщение о поиске реквизитов: {e}")

    # Отправляем новое сообщение с реквизитами
    data = await state.get_data()
    total = data.get("total", 0)
    await callback.message.answer(
        content.requisites_text(total),
        reply_markup=kb_paid_button(),
    )
    await state.set_state(Booking.waiting_paid_button)


@router.callback_query(F.data == "paid", Booking.waiting_paid_button)
async def cb_paid(callback: CallbackQuery, state: FSMContext):
    """Шаг 2: после нажатия «Я оплатил» просим прислать скрин перевода или PDF-чек."""
    await state.set_state(Booking.waiting_receipt)
    await safe_edit(callback.message, content.WAITING_RECEIPT_TEXT, kb_back())
    await callback.answer()


@router.message(Booking.waiting_receipt, F.photo)
@router.message(
    Booking.waiting_receipt,
    F.document & (F.document.mime_type == "application/pdf"),
)
async def cb_receipt_received(message: Message, state: FSMContext):
    """Шаг 3: получен скрин или PDF-чек — показываем сообщение о поиске платежа."""
    await state.clear()
    await message.answer(content.SEARCHING_PAYMENT_TEXT, reply_markup=kb_back())


@router.message(Booking.waiting_receipt)
async def cb_receipt_wrong_type(message: Message, state: FSMContext):
    """Если в состоянии ожидания чека прислали что-то другое — просим повторить."""
    await message.answer(content.WRONG_RECEIPT_TEXT, reply_markup=kb_back())


# ============================================================
#   ОБРАБОТЧИК НЕИЗВЕСТНЫХ КОМАНД / СООБЩЕНИЙ
# ============================================================

@router.message()
async def fallback_message(message: Message, state: FSMContext):
    await message.answer(
        "🤔 Я не понимаю это сообщение.\nВоспользуйтесь меню ниже 👇",
        reply_markup=kb_main_menu(),
    )


@router.callback_query()
async def fallback_callback(callback: CallbackQuery):
    await callback.answer("⚠️ Неизвестное действие. Попробуйте ещё раз.", show_alert=True)


# ============================================================
#   ВЕБ-СЕРВЕР: /ping + WEBHOOK + АВТОПИНГ
# ============================================================

async def ping_handler(request: web.Request) -> web.Response:
    return web.Response(text="pong")


async def ping_self():
    """Один вызов /ping по внешней ссылке проекта на Render (используется cron job'ом)."""
    if not RENDER_EXTERNAL_URL:
        return
    ping_url = f"{RENDER_EXTERNAL_URL}/ping"
    timeout = ClientTimeout(total=15)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(ping_url) as resp:
                logger.info(f"🔄 Cron-пинг {ping_url} -> статус {resp.status}")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка cron-пинга: {e}")


# Хранит объект cron job'а, чтобы он не был уничтожен сборщиком мусора
_cron_job = None


async def on_startup(bot: Bot):
    global _cron_job

    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан — webhook не установлен!")

    if RENDER_EXTERNAL_URL:
        # Настоящий cron job: пингует /ping по расписанию "*/10 * * * *" (каждые 10 минут)
        _cron_job = aiocron.crontab(PING_CRON_EXPRESSION, func=ping_self, start=True)
        logger.info(f"⏱️ Cron job запущен: {PING_CRON_EXPRESSION} -> {RENDER_EXTERNAL_URL}/ping")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан — cron-пинг отключён.")


async def on_shutdown(bot: Bot):
    # ВАЖНО: намеренно НЕ вызываем bot.delete_webhook() здесь.
    # Render разворачивает новый контейнер и только потом останавливает старый (rolling deploy).
    # Если удалять вебхук при остановке СТАРОГО контейнера, это удаляет вебхук,
    # который уже успел установить НОВЫЙ контейнер — и бот перестаёт получать апдейты
    # до следующего деплоя. set_webhook() при следующем старте всё равно переустановит его.
    global _cron_job
    if _cron_job is not None:
        _cron_job.stop()
        _cron_job = None
    logger.info("🛑 Бот останавливается (webhook не трогаем — это управляется on_startup).")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ping", ping_handler)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
