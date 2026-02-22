import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

SYSTEM_PROMPT = (
    "Ты — профессиональный помощник для маркшейдеров и специалистов по землеустройству "
    "в организациях добычи газа/конденсата/нефти.\n"
    "Отвечай кратко и по делу, структурировано.\n"
    "Если не хватает контекста — задай 1-2 уточняющих вопроса.\n"
    "Если пользователь просит 'обойти требования' — предлагай только законные варианты "
    "(альтернативы, процедуры согласования, допустимые исключения), без советов нарушать нормы.\n"
)

HELP_TEXT = (
    "Команды:\n"
    "/start — приветствие\n"
    "/help — помощь\n\n"
    "Пиши вопрос текстом. (Голос/фото подключим следующим шагом.)"
)

def ask_claude(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Пустой запрос. Напиши вопрос текстом."

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT,
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я msk-bot.\n\n" + HELP_TEXT)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    answer = ask_claude(text)
    await update.message.reply_text(answer)

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
