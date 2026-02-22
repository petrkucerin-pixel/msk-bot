import os
import logging

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

# --- ENV ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set (create .env on server)")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set (create .env on server)")

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("msk-bot")

# --- Claude ---
client = Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL = "claude-3-haiku-20240307"

SYSTEM_PROMPT_BASE = (
    "Ты — профессиональный помощник для маркшейдеров и специалистов по землеустройству "
    "в организациях добычи газа/конденсата/нефти.\n"
    "Отвечай кратко и по делу, структурировано.\n"
    "Если не хватает контекста — задай 1-2 уточняющих вопроса.\n"
    "Если пользователь просит 'обойти требования' — предлагай только законные варианты "
    "(альтернативы, процедуры согласования, допустимые исключения), без советов нарушать нормы.\n"
)

HELP_TEXT = (
    "Команды:\n"
    "/start — меню\n"
    "/menu — меню\n"
    "/help — помощь\n\n"
    "Выбирай раздел кнопками. Потом пиши запрос текстом."
)

# ---------------- UI ----------------

def kb_root() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏗️ Маркшейдерия", callback_data="root:mine")],
        [InlineKeyboardButton("🗺️ Землеустройство", callback_data="root:land")],
    ])

def kb_mine() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 Пересчёт координат", callback_data="mine:coords")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="mine:norms")],
        [InlineKeyboardButton("🧾 Составление отчёта", callback_data="mine:report")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:root")],
    ])

def kb_land() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Инфо по кадастровому номеру", callback_data="land:cadnum")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="land:norms")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:root")],
    ])

def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode

def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "none")

# ---------------- Claude wrapper ----------------

def ask_claude(text: str, system_add: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return "Пустой запрос. Напиши вопрос текстом."

    system = SYSTEM_PROMPT_BASE + (("\n" + system_add.strip()) if system_add.strip() else "")

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return ("\n".join(parts)).strip() or "Не получил текстовый ответ от модели."
    except Exception as e:
        logger.exception("Claude error")
        return f"Ошибка при обращении к Claude: {e}"

# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    await update.message.reply_text(
        "Выбери раздел:",
        reply_markup=kb_root()
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    await update.message.reply_text(
        "Меню. Выбери раздел:",
        reply_markup=kb_root()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    data = q.data or ""

    # Навигация
    if data == "nav:root":
        set_mode(context, "none")
        await q.edit_message_text("Выбери раздел:", reply_markup=kb_root())
        return

    if data == "root:mine":
        set_mode(context, "mine")
        await q.edit_message_text("Маркшейдерия — выбери действие:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        await q.edit_message_text("Землеустройство — выбери действие:", reply_markup=kb_land())
        return

    # Маркшейдерия
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        await q.edit_message_text(
            "📐 Пересчёт координат.\n"
            "Пришли текстом:\n"
            "1) какие системы (например: МСК ЯНАО -> WGS84)\n"
            "2) координаты (одна точка или список)\n\n"
            "Пока это заглушка. Следующим шагом подключим MAPINFOW.PRJ и pyproj.",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:norms":
        set_mode(context, "mine_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (маркшейдерия).\n"
            "Напиши запрос: что найти и по какому документу/теме.\n\n"
            "Пока отвечаю общими знаниями через Claude. Скоро подключим базу НД (поиск по пунктам).",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:report":
        set_mode(context, "mine_report")
        await q.edit_message_text(
            "🧾 Составление отчёта (Роснедра/карьеры).\n"
            "Напиши, какой отчёт нужен (например: 2-ГР / 5-гр / 7-ГР / 70-тп / 71-тп) и исходные данные.\n\n"
            "Пока заглушка. Следующим шагом подключим шаблоны и генерацию файлов.",
            reply_markup=kb_mine()
        )
        return

    # Землеустройство
    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        await q.edit_message_text(
            "🏷️ Инфо по кадастровому номеру.\n"
            "Пришли кадастровый номер текстом (пример: 89:00:000000:123).\n\n"
            "Пока заглушка. Далее подключим получение открытых сведений.",
            reply_markup=kb_land()
        )
        return

    if data == "land:norms":
        set_mode(context, "land_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (землеустройство).\n"
            "Напиши запрос: что найти.\n\n"
            "Список землеустроительных НД добавим позже — пока общий ответ через Claude.",
            reply_markup=kb_land()
        )
        return

    # На всякий случай
    await q.edit_message_text("Не понял команду. Открой /menu")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    text = update.message.text or ""

    # Если режим не выбран — мягко возвращаем в меню
    if mode in ("none", "mine", "land"):
        await update.message.reply_text("Сначала выбери раздел/действие:", reply_markup=kb_root())
        return

    # Далее — маршрутизация по режиму.
    # Пока: всё через Claude, но с правильной “рамкой” (системным дополнением).
    if mode == "mine_norms":
        system_add = "Режим: Маркшейдерия / Нормативная документация. Дай точный, практичный ответ."
        await update.message.reply_text(ask_claude(text, system_add=system_add))
        return

    if mode == "land_norms":
        system_add = "Режим: Землеустройство / Нормативная документация. Дай точный, практичный ответ."
        await update.message.reply_text(ask_claude(text, system_add=system_add))
        return

    if mode == "mine_coords":
        # Заглушка под будущий модуль трансформаций
        await update.message.reply_text(
            "Принял. Сейчас модуль пересчёта ещё не подключён.\n"
            "Следующий шаг: добавим MAPINFOW.PRJ + pyproj и сделаем реальный пересчёт.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    if mode == "mine_report":
        await update.message.reply_text(
            "Принял. Генератор отчётов ещё не подключён.\n"
            "Следующий шаг: подключим шаблоны Роснедра и начнём с 2-ГР/5-гр/7-ГР/70-тп/71-тп.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    if mode == "land_cadnum":
        await update.message.reply_text(
            "Принял кадастровый номер. Модуль кадастра ещё не подключён.\n"
            "Следующий шаг: подключим получение открытых сведений.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    # fallback
    await update.message.reply_text("Не понял режим. Нажми /menu")

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
