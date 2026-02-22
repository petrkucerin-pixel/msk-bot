import os
import re
import base64
import logging
from typing import Optional, Tuple, List, Dict, Any

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
    "В режимах координат/кадастра можно присылать фото. "
    "Я распознаю и попрошу подтвердить (чтобы не было 'додумывания')."
)

# ================== REGEX ==================
CADNUM_RE = re.compile(r"\b\d{2}:\d{2}:\d{6,7}:\d+\b")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

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
        [InlineKeyboardButton("🏠 Меню", callback_data="nav:root")],
    ])

def kb_land() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Инфо по кадастровому номеру", callback_data="land:cadnum")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="land:norms")],
        [InlineKeyboardButton("🏠 Меню", callback_data="nav:root")],
    ])

def kb_confirm(kind: str) -> InlineKeyboardMarkup:
    # kind: "coords" / "cadnum"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{kind}:ok")],
        [InlineKeyboardButton("✏️ Исправить вручную", callback_data=f"confirm:{kind}:edit")],
        [InlineKeyboardButton("🏠 Меню", callback_data="nav:root")],
    ])

# ================== STATE ==================
def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode

def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "none")

def clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending", None)
    context.user_data.pop("awaiting_manual_input", None)

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
    prompt_text = (prompt_text or "").strip() or "Распознай текст/цифры на изображении."

    system = SYSTEM_PROMPT_BASE + (("\n" + system_add.strip()) if system_add.strip() else "")

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

# ================== PARSING HELPERS ==================
def _clean_num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None

def parse_xy_from_text(text: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Пытаемся вытащить X и Y:
    - по шаблонам X=..., Y=...
    - или первые две большие цифры в тексте
    """
    t = text or ""

    # 1) X=..., Y=...
    mx = re.search(r"[XХ]\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    my = re.search(r"[YУ]\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    x = _clean_num(mx.group(1)) if mx else None
    y = _clean_num(my.group(1)) if my else None
    if x is not None and y is not None:
        return x, y

    # 2) просто два числа
    nums = NUM_RE.findall(t)
    if len(nums) >= 2:
        x2 = _clean_num(nums[0])
        y2 = _clean_num(nums[1])
        return x2, y2

    return None, None

def parse_cadnums_from_text(text: str) -> List[str]:
    return sorted(set(CADNUM_RE.findall(text or "")))

def is_plausible_coord(x: Optional[float], y: Optional[float]) -> bool:
    if x is None or y is None:
        return False
    # Очень грубая проверка "похоже на метры/координаты", без привязки к СК
    # (чтобы отсеять совсем мусор)
    return (abs(x) > 1000 and abs(y) > 1000)

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    context.user_data.pop("last_photo_b64", None)
    clear_pending(context)
    await update.message.reply_text("Выбери раздел:", reply_markup=kb_root())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    context.user_data.pop("last_photo_b64", None)
    clear_pending(context)
    await update.message.reply_text("Меню:", reply_markup=kb_root())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass  # защита от временных сетевых сбоев

    data = q.data or ""

    # --- глобальная навигация
    if data == "nav:root":
        set_mode(context, "none")
        context.user_data.pop("last_photo_b64", None)
        clear_pending(context)
        await q.edit_message_text("Выбери раздел:", reply_markup=kb_root())
        return

    # --- подтверждение распознавания
    if data.startswith("confirm:"):
        # confirm:{kind}:{action}
        _, kind, action = data.split(":", 2)
        pending = context.user_data.get("pending")

        if not pending or pending.get("kind") != kind:
            await q.edit_message_text("Нет данных для подтверждения. Открой /menu")
            return

        if action == "ok":
            # фиксируем как "принято"
            context.user_data["last_extracted"] = pending
            context.user_data.pop("pending", None)
            context.user_data.pop("awaiting_manual_input", None)

            if kind == "coords":
                x = pending.get("x")
                y = pending.get("y")
                await q.edit_message_text(
                    f"✅ Принято.\nX={x}\nY={y}\n\nСледующий шаг: подключим реальный пересчёт по MAPINFOW.PRJ.",
                    reply_markup=kb_mine()
                )
            else:
                cad = pending.get("cadnum")
                await q.edit_message_text(
                    f"✅ Принято.\nКН: {cad}\n\nСледующий шаг: подключим получение открытых сведений по КН.",
                    reply_markup=kb_land()
                )
            return

        if action == "edit":
            # ждём ручной ввод
            context.user_data["awaiting_manual_input"] = kind
            await q.edit_message_text(
                "✏️ Ок. Пришли одним сообщением правильные данные:\n"
                "- для координат: `X=... Y=...` или `... ...`\n"
                "- для кадастра: `89:xx:xxxxxx:xxx`\n\n"
                "Я подхвачу и обновлю.",
            )
            return

    # --- корневые разделы
    if data == "root:mine":
        set_mode(context, "mine")
        clear_pending(context)
        await q.edit_message_text("Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        clear_pending(context)
        await q.edit_message_text("Землеустройство:", reply_markup=kb_land())
        return

    # --- действия маркшейдерии
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        clear_pending(context)
        await q.edit_message_text(
            "📐 Пересчёт координат.\n"
            "Пришли координаты текстом или фото.\n"
            "После распознавания я попрошу подтвердить.\n\n"
            "Дальше подключим MAPINFOW.PRJ и сделаем реальный пересчёт.",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:norms":
        set_mode(context, "mine_norms")
        clear_pending(context)
        await q.edit_message_text(
            "📚 Нормативная документация (маркшейдерия).\n"
            "Напиши, что найти (пункт/тема/документ).",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:report":
        set_mode(context, "mine_report")
        clear_pending(context)
        await q.edit_message_text(
            "🧾 Составление отчёта (Роснедра).\n"
            "Укажи форму и исходные данные.\n"
            "(Пока заглушка, генерацию файлов подключим следующим шагом.)",
            reply_markup=kb_mine()
        )
        return

    # --- действия землеустройства
    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        clear_pending(context)
        await q.edit_message_text(
            "🏷️ Информация по кадастровому номеру.\n"
            "Пришли КН текстом или фото.\n"
            "После распознавания я попрошу подтвердить.\n\n"
            "Дальше подключим получение открытых сведений по КН.",
            reply_markup=kb_land()
        )
        return

    if data == "land:norms":
        set_mode(context, "land_norms")
        clear_pending(context)
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

    # --- если ждём ручной ввод после "Исправить"
    awaiting = context.user_data.get("awaiting_manual_input")
    if awaiting == "coords":
        x, y = parse_xy_from_text(text)
        if is_plausible_coord(x, y):
            context.user_data["pending"] = {"kind": "coords", "x": x, "y": y, "source": "manual"}
            context.user_data.pop("awaiting_manual_input", None)
            await update.message.reply_text(
                f"Я понял так:\nX={x}\nY={y}\nПодтверждаешь?",
                reply_markup=kb_confirm("coords")
            )
        else:
            await update.message.reply_text(
                "Не смог понять координаты. Пришли в формате:\n"
                "X=72853345 Y=551668\n"
                "или двумя числами через пробел."
            )
        return

    if awaiting == "cadnum":
        cadnums = parse_cadnums_from_text(text)
        if len(cadnums) == 1:
            cad = cadnums[0]
            context.user_data["pending"] = {"kind": "cadnum", "cadnum": cad, "source": "manual"}
            context.user_data.pop("awaiting_manual_input", None)
            await update.message.reply_text(
                f"Я понял так:\nКН: {cad}\nПодтверждаешь?",
                reply_markup=kb_confirm("cadnum")
            )
        elif len(cadnums) > 1:
            await update.message.reply_text(
                "Нашёл несколько КН:\n- " + "\n- ".join(cadnums) + "\n\nПришли один нужный."
            )
        else:
            await update.message.reply_text(
                "Не вижу корректный кадастровый номер (формат типа 89:35:800113:31). "
                "Пришли ещё раз."
            )
        return

    # --- если ранее прислали фото без подписи и теперь пришёл текст (задача к фото)
    last_photo_b64 = context.user_data.pop("last_photo_b64", None)
    if last_photo_b64:
        await update.message.reply_text("Принял подпись. Анализирую фото…")
        result_text, reply_markup = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=last_photo_b64,
            caption=text,
        )
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

    # --- если режим не выбран
    if mode in ("none", "mine", "land"):
        await update.message.reply_text("Сначала выбери раздел/действие:", reply_markup=kb_root())
        return

    # --- нормативка (пока текстом через Claude)
    if mode == "mine_norms":
        await update.message.reply_text(
            ask_claude_text(text, "Режим: маркшейдерская нормативка."),
            reply_markup=kb_mine()
        )
        return

    if mode == "land_norms":
        await update.message.reply_text(
            ask_claude_text(text, "Режим: землеустроительная нормативка."),
            reply_markup=kb_land()
        )
        return

    # --- кадастр: если прислали текст, но формат неправильный — не додумываем
    if mode == "land_cadnum":
        cadnums = parse_cadnums_from_text(text)
        if len(cadnums) == 1:
            cad = cadnums[0]
            context.user_data["pending"] = {"kind": "cadnum", "cadnum": cad, "source": "text"}
            await update.message.reply_text(
                f"Я распознал кадастровый номер как:\n{cad}\nПодтверждаешь?",
                reply_markup=kb_confirm("cadnum")
            )
        elif len(cadnums) > 1:
            await update.message.reply_text(
                "Нашёл несколько КН:\n- " + "\n- ".join(cadnums) + "\n\nПришли один нужный.",
                reply_markup=kb_land()
            )
        else:
            await update.message.reply_text(
                "Не вижу корректный кадастровый номер.\n"
                "Ожидаемый формат: 89:35:800113:31\n"
                "Если у тебя запись без двоеточий/с ошибками — пришли фото, либо напиши ещё раз.",
                reply_markup=kb_land()
            )
        return

    # --- заглушки
    if mode == "mine_report":
        await update.message.reply_text(
            "Принял. Генератор отчётов ещё не подключён.\n"
            "Следующий шаг: подключим шаблоны и генерацию файлов.\n\n"
            f"Твой ввод:\n{text}",
            reply_markup=kb_mine()
        )
        return

    if mode == "mine_coords":
        x, y = parse_xy_from_text(text)
        if is_plausible_coord(x, y):
            context.user_data["pending"] = {"kind": "coords", "x": x, "y": y, "source": "text"}
            await update.message.reply_text(
                f"Я понял координаты так:\nX={x}\nY={y}\nПодтверждаешь?",
                reply_markup=kb_confirm("coords")
            )
        else:
            await update.message.reply_text(
                "Принял. Если координаты на фото — пришли фото, я распознаю и попрошу подтвердить.\n"
                "Если текстом — пришли в формате `X=... Y=...` или двумя числами через пробел.",
                reply_markup=kb_mine()
            )
        return

    await update.message.reply_text("Не понял режим. Нажми /menu", reply_markup=kb_root())

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    caption = (update.message.caption or "").strip()

    photo = update.message.photo[-1]
    f = await photo.get_file()
    b = await f.download_as_bytearray()
    image_b64 = base64.b64encode(bytes(b)).decode("utf-8")

    # Если подпись есть — сразу
    if caption:
        await update.message.reply_text("Фото с подписью получено. Анализирую…")
        result_text, reply_markup = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=image_b64,
            caption=caption,
        )
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

    # В режимах координат/кадастра — обрабатываем и без подписи (по контексту)
    if mode in ("mine_coords", "land_cadnum"):
        await update.message.reply_text("Фото получил. Пробую распознать…")
        result_text, reply_markup = await process_photo_in_mode(
            context=context,
            mode=mode,
            image_b64=image_b64,
            caption="",
        )
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

    # Иначе — ждём уточнение
    context.user_data["last_photo_b64"] = image_b64
    await update.message.reply_text(
        "Фото получил. Напиши одним сообщением, что нужно извлечь/сделать по этому фото.\n"
        "Или выбери режим через /menu."
    )

async def process_photo_in_mode(
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    image_b64: str,
    caption: str,
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Возвращает (текст, клавиатура).
    Сохраняет распознанное в context.user_data["pending"] для подтверждения.
    """

    cap_low = (caption or "").lower()

    # ====== Координаты ======
    if mode == "mine_coords" or ("коорд" in cap_low) or ("x=" in cap_low) or ("y=" in cap_low):
        system_add = (
            "Задача: распознать координаты X и Y с изображения.\n"
            "КРИТИЧНО:\n"
            "- Не выдумывай и не 'додумывай' цифры.\n"
            "- Если символ/цифра плохо видна — поставь знак '?' на её месте.\n"
            "- Верни ровно в формате:\n"
            "TRANSCRIPTION:\n"
            "<перепиши как на бумаге, строка в строку>\n"
            "PARSED:\n"
            "X=<значение как видишь>\n"
            "Y=<значение как видишь>\n"
            "Если не уверен — в значении оставь '?'.\n"
        )
        prompt = caption.strip() or "Распознай X и Y."
        raw = ask_claude_with_image(prompt, image_b64, system_add=system_add)

        # Пытаемся вытащить X/Y из блока PARSED
        # (если есть ?, то парсинг в float не пройдёт — и это хорошо: попросим подтвердить)
        x_s = None
        y_s = None
        mx = re.search(r"\bX\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        my = re.search(r"\bY\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        if mx:
            x_s = mx.group(1).strip()
        if my:
            y_s = my.group(1).strip()

        # fallback: пробуем достать числа из всего текста
        x_val = _clean_num(x_s) if x_s and "?" not in x_s else None
        y_val = _clean_num(y_s) if y_s and "?" not in y_s else None

        if x_val is None or y_val is None or not is_plausible_coord(x_val, y_val):
            # не уверены — показываем как распознали (сырьё) и просим подтвердить/исправить
            context.user_data["pending"] = {
                "kind": "coords",
                "x": x_val,
                "y": y_val,
                "raw": raw,
                "source": "photo",
            }
            msg = (
                "Я распознал координаты с фото, но есть неуверенность.\n\n"
                f"{raw}\n\n"
                "Проверь. Если верно — нажми ✅ Подтвердить.\n"
                "Если неверно — ✏️ Исправить вручную."
            )
            return msg, kb_confirm("coords")

        # уверенно распарсили
        context.user_data["pending"] = {
            "kind": "coords",
            "x": x_val,
            "y": y_val,
            "raw": raw,
            "source": "photo",
        }
        msg = (
            "Я распознал так:\n"
            f"X={x_val}\nY={y_val}\n\n"
            "Подтверждаешь?"
        )
        return msg, kb_confirm("coords")

    # ====== Кадастровый номер ======
    if mode == "land_cadnum" or ("кадастр" in cap_low) or ("кн" in cap_low):
        system_add = (
            "Задача: распознать кадастровый номер РФ на изображении.\n"
            "КРИТИЧНО:\n"
            "- Не выдумывай и не исправляй номер.\n"
            "- Если не уверен в цифре — поставь '?' на её месте.\n"
            "- Верни в формате:\n"
            "TRANSCRIPTION:\n"
            "<как написано>\n"
            "PARSED:\n"
            "CADNUM=<как распознал>\n"
        )
        prompt = caption.strip() or "Распознай кадастровый номер."
        raw = ask_claude_with_image(prompt, image_b64, system_add=system_add)

        mc = re.search(r"\bCADNUM\s*=\s*([0-9?:]+)", raw, re.IGNORECASE)
        cad_guess = mc.group(1).strip() if mc else ""

        # Если cad_guess уже похож на нормальный кадастровый номер — ок, но всё равно просим подтвердить
        cadnums = parse_cadnums_from_text(cad_guess) if cad_guess and "?" not in cad_guess else []
        if len(cadnums) == 1:
            cad = cadnums[0]
            context.user_data["pending"] = {"kind": "cadnum", "cadnum": cad, "raw": raw, "source": "photo"}
            return f"Я распознал КН как:\n{cad}\nПодтверждаешь?", kb_confirm("cadnum")

        # Иначе — ничего не додумываем
        context.user_data["pending"] = {"kind": "cadnum", "cadnum": None, "raw": raw, "source": "photo"}
        msg = (
            "Я попытался распознать КН, но не уверен.\n\n"
            f"{raw}\n\n"
            "Проверь. Если правильно — нажми ✅ Подтвердить (если CADNUM в формате верный).\n"
            "Если неправильно — ✏️ Исправить вручную."
        )
        return msg, kb_confirm("cadnum")

    # ====== Неясно ======
    return (
        "Не понял, что извлекать с фото.\n"
        "Выбери режим:\n"
        "- Маркшейдерия → Пересчёт координат\n"
        "- Землеустройство → Инфо по кадастровому номеру\n"
        "или пришли подпись к фото, что именно нужно.",
        kb_root()
    )

# ================== ERROR HANDLER ==================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Произошёл сетевой/временный сбой. Повтори действие.")
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
