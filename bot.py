import os
import re
import base64
import logging
from typing import Optional

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

HELP_TEXT = (
    "Команды:\n"
    "/start — меню\n"
    "/menu — меню\n"
    "/help — помощь\n\n"
    "Выбирай раздел кнопками. В режимах координат/кадастра можно присылать фото — "
    "я постараюсь извлечь цифры."
)

# ================== KEYBOARDS ==================
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

# ================== STATE ==================
def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode

def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "none")

# ================== CLAUDE CALLS ==================
def ask_claude_text(text: str, system_add: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return "Пустой запрос."

    system = SYSTEM_PROMPT_BASE + (("\n" + system_add.strip()) if system_add.strip() else "")

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        out = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out.append(block.text)
        return "\n".join(out).strip() or "Не получил текстовый ответ от модели."
    except Exception as e:
        logger.exception("Claude error (text)")
        return f"Ошибка Claude: {e}"

def ask_claude_with_image(prompt_text: str, image_b64: str, system_add: str = "") -> str:
    prompt_text = (prompt_text or "").strip()
    if not prompt_text:
        prompt_text = "Извлеки данные с изображения."

    system = SYSTEM_PROMPT_BASE + (("\n" + system_add.strip()) if system_add.strip() else "")

    # Claude Vision: content = [image, text]
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        },
        {"type": "text", "text": prompt_text},
    ]

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=900,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        out = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out.append(block.text)
        return "\n".join(out).strip() or "Не получил текстовый ответ от модели."
    except Exception as e:
        logger.exception("Claude error (image)")
        return f"Ошибка Claude (image): {e}"

# ================== PARSERS (MVP) ==================
CADNUM_RE = re.compile(r"\b\d{2}:\d{2}:\d{6,7}:\d+\b")

def extract_cadnums_from_text(t: str) -> list[str]:
    return sorted(set(CADNUM_RE.findall(t or "")))

def looks_like_coord_line(line: str) -> bool:
    # очень грубо: 2–3 числа с точкой/запятой
    nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?", line)
    return len(nums) >= 2

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    context.user_data.pop("last_photo_b64", None)
    context.user_data.pop("last_extracted", None)
    await update.message.reply_text("Выбери раздел:", reply_markup=kb_root())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    context.user_data.pop("last_photo_b64", None)
    await update.message.reply_text("Меню:", reply_markup=kb_root())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        # защита от временных сетевых сбоев (httpx.ReadError)
        pass

    data = q.data or ""

    if data == "nav:root":
        set_mode(context, "none")
        context.user_data.pop("last_photo_b64", None)
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
        context.user_data.pop("last_photo_b64", None)
        await q.edit_message_text(
            "📐 Пересчёт координат.\n"
            "Пришли координаты текстом или фото (таблица/скрин).\n\n"
            "Сейчас я умею: извлечь координаты с фото.\n"
            "Следующий шаг: подключим MAPINFOW.PRJ и сделаем реальный пересчёт.",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:norms":
        set_mode(context, "mine_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (маркшейдерия).\n"
            "Напиши, что найти (пункт/тема/документ).",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:report":
        set_mode(context, "mine_report")
        await q.edit_message_text(
            "🧾 Составление отчёта (Роснедра).\n"
            "Укажи форму и исходные данные.\n"
            "(Пока заглушка, файлы будем генерировать следующим шагом.)",
            reply_markup=kb_mine()
        )
        return

    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        context.user_data.pop("last_photo_b64", None)
        await q.edit_message_text(
            "🏷️ Информация по кадастровому номеру.\n"
            "Пришли КН текстом или фото (скрин/фото документа).\n\n"
            "Сейчас я умею: извлечь КН с фото.\n"
            "Следующий шаг: подключим получение открытых сведений.",
            reply_markup=kb_land()
        )
        return

    if data == "land:norms":
        set_mode(context, "land_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (землеустройство).\n"
            "Напиши запрос (позже добавим список землеустроительных НД).",
            reply_markup=kb_land()
        )
        return

    await q.edit_message_text("Не понял команду. Открой /menu")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    text = update.message.text or ""

    # Если есть фото, которое пришло без подписи — используем это сообщение как "задачу к фото"
    last_photo_b64 = context.user_data.pop("last_photo_b64", None)
    if last_photo_b64:
        await update.message.reply_text("Принял подпись. Анализирую фото…")
        # Определим, что извлекать, исходя из режима, либо по тексту
        result = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=last_photo_b64,
            caption=text,
        )
        await update.message.reply_text(result)
        return

    # Если режим не выбран — отправим в меню
    if mode in ("none", "mine", "land"):
        await update.message.reply_text("Сначала выбери раздел/действие:", reply_markup=kb_root())
        return

    # Нормативка пока через Claude-текст
    if mode == "mine_norms":
        await update.message.reply_text(
            ask_claude_text(text, "Режим: маркшейдерская нормативка.")
        )
        return

    if mode == "land_norms":
        await update.message.reply_text(
            ask_claude_text(text, "Режим: землеустроительная нормативка.")
        )
        return

    # Заглушки (пока без вычислений/кадастра)
    if mode == "mine_report":
        await update.message.reply_text(
            "Принял. Генератор отчётов ещё не подключён.\n"
            "Следующий шаг: подключим шаблоны и генерацию файлов.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    if mode == "mine_coords":
        await update.message.reply_text(
            "Принял. Сейчас модуль пересчёта ещё не подключён.\n"
            "Но если у тебя координаты на фото — пришли фото, я вытащу числа.\n\n"
            f"Твой ввод:\n{text}"
        )
        return

    if mode == "land_cadnum":
        # здесь можно хотя бы вытащить КН из текста
        cadnums = extract_cadnums_from_text(text)
        if cadnums:
            context.user_data["last_extracted"] = {"type": "cadnum", "values": cadnums}
            await update.message.reply_text(
                "Нашёл кадастровые номера:\n- " + "\n- ".join(cadnums) +
                "\n\nСледующий шаг: подключим получение открытых сведений по КН."
            )
        else:
            await update.message.reply_text(
                "Не вижу кадастровый номер в сообщении.\n"
                "Формат обычно такой: 89:00:000000:123\n"
                "Можно прислать фото — я попробую извлечь."
            )
        return

    await update.message.reply_text("Не понял режим. Нажми /menu")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    caption = (update.message.caption or "").strip()

    # Скачиваем фото (самое большое)
    photo = update.message.photo[-1]
    f = await photo.get_file()
    b = await f.download_as_bytearray()
    image_b64 = base64.b64encode(bytes(b)).decode("utf-8")

    # Если подпись есть — сразу обрабатываем
    if caption:
        await update.message.reply_text("Фото с подписью получено. Анализирую…")
        result = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=image_b64,
            caption=caption,
        )
        await update.message.reply_text(result)
        return

    # Если подписи нет:
    # В режимах координат/кадастра можно обрабатывать и без подписи (по контексту)
    if mode in ("mine_coords", "land_cadnum"):
        await update.message.reply_text("Фото получил. Пробую извлечь данные…")
        result = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=image_b64,
            caption="",
        )
        await update.message.reply_text(result)
        return

    # Иначе — попросим уточнение и запомним фото
    context.user_data["last_photo_b64"] = image_b64
    await update.message.reply_text(
        "Фото получил. Напиши одним сообщением, что нужно извлечь/сделать по этому фото."
    )

async def process_photo_in_mode(
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    image_b64: str,
    caption: str,
) -> str:
    """
    Возвращает текст результата и сохраняет extracted в context.user_data["last_extracted"].
    """

    # 1) Координаты
    if mode == "mine_coords" or ("коорд" in (caption or "").lower()):
        system_add = (
            "Задача: извлечь координаты с изображения.\n"
            "Верни ответ строго в формате:\n"
            "КОММЕНТАРИЙ: 1-3 строки\n"
            "ДАННЫЕ:\n"
            "N;X;Y;Z\n"
            "1;...;...;...\n"
            "2;...;...;...\n"
            "Если Z нет — ставь пусто.\n"
            "Если вместо X/Y на изображении широта/долгота — всё равно выведи как X=lon, Y=lat и напиши это в комментарии.\n"
            "Не выдумывай числа."
        )
        prompt = caption.strip() or "Извлеки координаты (таблица/список) с изображения."
        raw = ask_claude_with_image(prompt, image_b64, system_add=system_add)

        # Сохраним сырой результат как extracted (потом парсинг улучшим)
        context.user_data["last_extracted"] = {"type": "coords", "raw": raw}
        return raw + "\n\n(Дальше подключим пересчёт по MAPINFOW.PRJ.)"

    # 2) Кадастровые номера
    if mode == "land_cadnum" or ("кадастр" in (caption or "").lower()) or ("кадастров" in (caption or "").lower()):
        system_add = (
            "Задача: извлечь кадастровые номера РФ с изображения.\n"
            "Верни ответ строго в формате:\n"
            "НАЙДЕНО:\n"
            "- 89:..:......:...\n"
            "- ...\n"
            "Если ничего нет — верни:\n"
            "НАЙДЕНО:\n"
            "- (нет)\n"
            "Не выдумывай."
        )
        prompt = caption.strip() or "Найди и выпиши все кадастровые номера на изображении."
        raw = ask_claude_with_image(prompt, image_b64, system_add=system_add)

        # Дополнительно попробуем регуляркой вытащить КН
        cadnums = extract_cadnums_from_text(raw)
        if cadnums:
            context.user_data["last_extracted"] = {"type": "cadnum", "values": cadnums}
            return "Нашёл кадастровые номера:\n- " + "\n- ".join(cadnums) + "\n\n(Дальше подключим получение открытых сведений.)"

        context.user_data["last_extracted"] = {"type": "cadnum", "raw": raw}
        return raw + "\n\n(Дальше подключим получение открытых сведений.)"

    # 3) Неясно что делать
    return (
        "Не понял, что извлекать с фото.\n"
        "Выбери режим:\n"
        "- Маркшейдерия → Пересчёт координат (и пришли фото)\n"
        "- Землеустройство → Инфо по кадастровому номеру (и пришли фото)\n"
        "или пришли подпись к фото, что именно нужно."
    )

# ================== ERROR HANDLER ==================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Произошёл сетевой/временный сбой. Повтори действие."
            )
    except Exception:
        pass

# ================== MAIN ==================
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
