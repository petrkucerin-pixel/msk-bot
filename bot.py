import os
import logging

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

# ================== ENV ==================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set")

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("msk-bot")

# ================== CLAUDE ==================
client = Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL = "claude-3-haiku-20240307"

SYSTEM_PROMPT_BASE = (
    "Ты — профессиональный помощник для маркшейдеров и специалистов по землеустройству "
    "в организациях добычи газа, конденсата и нефти.\n"
    "Отвечай строго по делу, кратко и структурировано.\n"
    "Если не хватает данных — задай уточняющие вопросы.\n"
    "Если спрашивают про обход требований — предлагай ТОЛЬКО законные варианты "
    "(альтернативы, согласования, допустимые исключения).\n"
)

# ================== KEYBOARDS ==================
def kb_root():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏗️ Маркшейдерия", callback_data="root:mine")],
        [InlineKeyboardButton("🗺️ Землеустройство", callback_data="root:land")],
    ])

def kb_mine():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 Пересчёт координат", callback_data="mine:coords")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="mine:norms")],
        [InlineKeyboardButton("🧾 Составление отчёта", callback_data="mine:report")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:root")],
    ])

def kb_land():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Инфо по кадастровому номеру", callback_data="land:cadnum")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="land:norms")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="nav:root")],
    ])

# ================== STATE ==================
def set_mode(context, mode):
    context.user_data["mode"] = mode

def get_mode(context):
    return context.user_data.get("mode", "none")

# ================== CLAUDE CALL ==================
def ask_claude(text: str, system_add: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return "Пустой запрос."

    system = SYSTEM_PROMPT_BASE
    if system_add:
        system += "\n" + system_add

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        out = []
        for block in resp.content:
            if block.type == "text":
                out.append(block.text)
        return "\n".join(out).strip()
    except Exception as e:
        logger.exception("Claude error")
        return f"Ошибка Claude: {e}"

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_mode(context, "none")
    await update.message.reply_text("Выбери раздел:", reply_markup=kb_root())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_mode(context, "none")
    await update.message.reply_text("Меню:", reply_markup=kb_root())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass  # защита от httpx.ReadError

    data = q.data

    if data == "nav:root":
        set_mode(context, "none")
        await q.edit_message_text("Выбери раздел:", reply_markup=kb_root())
        return

    if data == "root:mine":
        set_mode(context, "mine")
        await q.edit_message_text("Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        await q.edit_message_text("Землеустройство:", reply_markup=kb_land())
        return

    if data == "mine:coords":
        set_mode(context, "mine_coords")
        await q.edit_message_text(
            "📐 Пересчёт координат.\n"
            "Пришли координаты (можно фото).\n"
            "Модуль будет подключён следующим шагом.",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:norms":
        set_mode(context, "mine_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (маркшейдерия).\n"
            "Напиши, что найти.",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:report":
        set_mode(context, "mine_report")
        await q.edit_message_text(
            "🧾 Составление отчёта (Роснедра).\n"
            "Укажи форму и исходные данные.",
            reply_markup=kb_mine()
        )
        return

    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        await q.edit_message_text(
            "🏷️ Информация по кадастровому номеру.\n"
            "Пришли номер (можно фото).",
            reply_markup=kb_land()
        )
        return

    if data == "land:norms":
        set_mode(context, "land_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (землеустройство).\n"
            "Напиши запрос.",
            reply_markup=kb_land()
        )
        return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = get_mode(context)
    text = update.message.text or ""

    if mode == "mine_norms":
        await update.message.reply_text(
            ask_claude(text, "Режим: маркшейдерская нормативка.")
        )
        return

    if mode == "land_norms":
        await update.message.reply_text(
            ask_claude(text, "Режим: землеустроительная нормативка.")
        )
        return

    if mode in ("mine_coords", "mine_report", "land_cadnum"):
        await update.message.reply_text(
            "Функция ещё не подключена.\n"
            "Следующим шагом сделаем полноценный модуль.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    await update.message.reply_text("Выбери раздел через /menu")

# ================== ERROR HANDLER ==================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Произошёл сетевой сбой. Повтори действие."
            )
    except Exception:
        pass

# ================== MAIN ==================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
