import os
import json
import logging
from datetime import date
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ================== ENV ==================
load_dotenv()
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
if not ADMIN_BOT_TOKEN:
    raise RuntimeError("ADMIN_BOT_TOKEN not set")

# ================== CONSTANTS ==================
ADMIN_ID = 1306327841

# Файлы основного бота (должны быть в той же папке)
USAGE_FILE = "usage.json"
USERS_FILE = "users.json"

# Цены claude-3-haiku-20240307 ($ за 1M токенов)
PRICE_INPUT_PER_1M = 0.25
PRICE_OUTPUT_PER_1M = 1.25

# Примерное соотношение input/output токенов
AVG_INPUT_TOKENS = 800   # системный промпт + история + вопрос
AVG_OUTPUT_TOKENS = 400  # ответ

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("admin-bot")


# ================== HELPERS ==================
def load_usage() -> dict:
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def load_users() -> list:
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def calc_cost(requests: int) -> float:
    """Примерная стоимость в USD за количество запросов."""
    input_tokens = requests * AVG_INPUT_TOKENS
    output_tokens = requests * AVG_OUTPUT_TOKENS
    cost = (input_tokens / 1_000_000 * PRICE_INPUT_PER_1M +
            output_tokens / 1_000_000 * PRICE_OUTPUT_PER_1M)
    return cost

def format_stats() -> str:
    usage = load_usage()
    users = load_users()
    today = date.today().isoformat()

    total_today = 0
    total_all = 0
    user_lines = []

    for user_id, data in usage.items():
        count_today = data.get("count", 0) if data.get("date") == today else 0
        # Суммируем все запросы за всё время (храним накопленно)
        count_total = data.get("total", count_today)
        total_today += count_today
        total_all += count_total

        cost_today = calc_cost(count_today)
        cost_total = calc_cost(count_total)

        if count_today > 0 or count_total > 0:
            user_lines.append(
                f"👤 ID {user_id}\n"
                f"   Сегодня: {count_today} зап. (~${cost_today:.4f})\n"
                f"   Всего: {count_total} зап. (~${cost_total:.3f})"
            )

    cost_today_total = calc_cost(total_today)
    cost_all_total = calc_cost(total_all)

    lines = [
        f"📊 Статистика MSK-Bot",
        f"📅 Дата: {today}",
        f"👥 Всего пользователей: {len(users)}",
        "",
        "─── По пользователям ───",
    ]

    if user_lines:
        lines += user_lines
    else:
        lines.append("Нет данных")

    lines += [
        "",
        "─── Итого ───",
        f"Сегодня: {total_today} запросов (~${cost_today_total:.4f})",
        f"Всего: {total_all} запросов (~${cost_all_total:.3f})",
        "",
        f"💡 Цены: claude-3-haiku",
        f"   Input: ${PRICE_INPUT_PER_1M}/1M токенов",
        f"   Output: ${PRICE_OUTPUT_PER_1M}/1M токенов",
        f"   ~{AVG_INPUT_TOKENS} вх. + {AVG_OUTPUT_TOKENS} исх. токенов/запрос",
    ]

    return "\n".join(lines)


# ================== HANDLERS ==================
def admin_only(func):
    """Декоратор — только для ADMIN_ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Нет доступа.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я бот-админ MSK-Bot.\n\n"
        "Команды:\n"
        "/stats — статистика запросов и расходов\n"
        "/users — список пользователей\n"
        "/today — только сегодняшние данные"
    )


@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_stats()
    await update.message.reply_text(text)


@admin_only
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = load_users()
    if not users:
        await update.message.reply_text("Пользователей пока нет.")
        return
    lines = [f"👥 Пользователи MSK-Bot ({len(users)} чел.):"]
    for i, uid in enumerate(users, 1):
        lines.append(f"{i}. ID: {uid}")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    usage = load_usage()
    today_date = date.today().isoformat()
    lines = [f"📅 Активность сегодня ({today_date}):"]
    total = 0
    for user_id, data in usage.items():
        if data.get("date") == today_date:
            count = data.get("count", 0)
            total += count
            cost = calc_cost(count)
            lines.append(f"👤 {user_id}: {count} зап. (~${cost:.4f})")
    lines.append(f"\nИтого: {total} запросов (~${calc_cost(total):.4f})")
    await update.message.reply_text("\n".join(lines))


# ================== MAIN ==================
def main() -> None:
    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("today", today))

    logger.info("admin-bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
