import os
import re
import base64
import logging
from typing import Optional, Tuple, List, Dict, Any

import httpx
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

from pyproj import CRS, Transformer


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
    "Координаты: можно фото или ручной ввод. Для пересчёта задай СК строкой:\n"
    "СК: EPSG:3857 -> EPSG:4326\n"
    "или\n"
    "СК: WGS84 -> WebMercator\n\n"
    "Кадастр: можно фото или ручной ввод (КН в формате 89:35:800113:31)."
)


# ================== REGEX ==================
CADNUM_RE = re.compile(r"\b\d{2}:\d{2}:\d{6,7}:\d+\b")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


# ================== CRS ALIASES ==================
CRS_ALIASES = {
    "WGS84": "EPSG:4326",
    "WGS 84": "EPSG:4326",
    "EPSG4326": "EPSG:4326",
    "WEBMERCATOR": "EPSG:3857",
    "WEB MERCATOR": "EPSG:3857",
    "EPSG3857": "EPSG:3857",
    "PULKOVO42": "EPSG:4284",   # геогр. Пулково 1942 (если нужно)
    "GSK2011": "EPSG:7683",    # часто встречается как ГСК-2011 (в proj.db может отличаться)
    "ГСК2011": "EPSG:7683",
    "ГСК-2011": "EPSG:7683",
}


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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{kind}:ok")],
        [InlineKeyboardButton("✏️ Исправить вручную", callback_data=f"confirm:{kind}:edit")],
        [InlineKeyboardButton("🏠 Меню", callback_data="nav:root")],
    ])


def kb_mode_actions_coords() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Задать СК", callback_data="manual:set_crs")],
        [InlineKeyboardButton("✍️ Ввести координаты вручную", callback_data="manual:coords")],
        [InlineKeyboardButton("🏠 Меню", callback_data="nav:root")],
    ])


def kb_mode_actions_cadnum() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Ввести КН вручную", callback_data="manual:cadnum")],
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


def clear_photo_stash(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("last_photo_b64", None)


def set_crs_pair(context: ContextTypes.DEFAULT_TYPE, src: str, dst: str) -> None:
    context.user_data["coords_src_crs"] = src
    context.user_data["coords_dst_crs"] = dst


def get_crs_pair(context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[str], Optional[str]]:
    return context.user_data.get("coords_src_crs"), context.user_data.get("coords_dst_crs")


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
    t = text or ""
    mx = re.search(r"[XХ]\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    my = re.search(r"[YУ]\s*[:=]\s*([-+]?\d+(?:[.,]\d+)?)", t, re.IGNORECASE)
    x = _clean_num(mx.group(1)) if mx else None
    y = _clean_num(my.group(1)) if my else None
    if x is not None and y is not None:
        return x, y

    nums = NUM_RE.findall(t)
    if len(nums) >= 2:
        return _clean_num(nums[0]), _clean_num(nums[1])
    return None, None


def parse_points_from_text(text: str) -> List[Tuple[float, float]]:
    """
    Принимает:
    - одну точку: "X=... Y=..." или "x y"
    - несколько строк: каждая строка содержит минимум 2 числа (возьмём первые 2)
    """
    points: List[Tuple[float, float]] = []

    # Если есть явное X=...Y=...
    x, y = parse_xy_from_text(text)
    if x is not None and y is not None:
        return [(x, y)]

    # Иначе по строкам: берём первые два числа в каждой строке
    for line in (text or "").splitlines():
        nums = NUM_RE.findall(line)
        if len(nums) >= 2:
            x2 = _clean_num(nums[0])
            y2 = _clean_num(nums[1])
            if x2 is not None and y2 is not None:
                points.append((x2, y2))
    return points


def parse_cadnums_from_text(text: str) -> List[str]:
    return sorted(set(CADNUM_RE.findall(text or "")))


def is_plausible_coord(x: Optional[float], y: Optional[float]) -> bool:
    if x is None or y is None:
        return False
    return (abs(x) > 1 and abs(y) > 1)


def normalize_crs_input(s: str) -> str:
    s2 = (s or "").strip()
    if not s2:
        return s2
    up = s2.upper().replace(" ", "")
    if up in CRS_ALIASES:
        return CRS_ALIASES[up]
    # если человек написал "EPSG:XXXX" оставляем как есть
    return s2


def parse_crs_pair_from_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Ожидается:
      "СК: <src> -> <dst>"
    или:
      "<src> -> <dst>"
    """
    t = (text or "").strip()
    t = t.replace("—", "->").replace("→", "->")
    t = re.sub(r"^\s*СК\s*:\s*", "", t, flags=re.IGNORECASE)
    if "->" not in t:
        return None, None
    left, right = t.split("->", 1)
    src = normalize_crs_input(left.strip())
    dst = normalize_crs_input(right.strip())
    if not src or not dst:
        return None, None
    return src, dst


def build_transformer(src: str, dst: str) -> Transformer:
    crs_src = CRS.from_user_input(src)
    crs_dst = CRS.from_user_input(dst)
    return Transformer.from_crs(crs_src, crs_dst, always_xy=True)


def format_points_table(points: List[Tuple[float, float]]) -> str:
    lines = ["N;X;Y"]
    for i, (x, y) in enumerate(points, start=1):
        lines.append(f"{i};{x};{y}")
    return "\n".join(lines)


# ================== CADASTRE (NSPD) ==================
async def fetch_nspd_info(cadnum: str) -> Dict[str, Any]:
    """
    НСПД часто требует Referer. По форумам рабочий урл:
    https://nspd.gov.ru/api/geoportal/v2/search/geoportal?thematicSearchId=1&query=<КН>
    """
    url = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
    params = {"thematicSearchId": "1", "query": cadnum}

    headers = {
        # реферер часто критичен
        "Referer": "https://nspd.gov.ru/map?thematic=PKK",
        "User-Agent": "msk-bot/1.0 (+telegram)",
        "Accept": "application/json, text/plain, */*",
    }

    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def _find_first(d: Any, keys: List[str]) -> Optional[Any]:
    """
    Рекурсивно ищем первое значение по набору ключей (case-insensitive).
    """
    if isinstance(d, dict):
        for k, v in d.items():
            if k and isinstance(k, str) and k.lower() in keys:
                return v
        for v in d.values():
            got = _find_first(v, keys)
            if got is not None:
                return got
    elif isinstance(d, list):
        for item in d:
            got = _find_first(item, keys)
            if got is not None:
                return got
    return None


def summarize_nspd_json(cadnum: str, data: Dict[str, Any]) -> str:
    # Пытаемся вытащить часто встречающиеся поля
    found_cad = _find_first(data, ["cadastralnumber", "cadnum", "cadastr", "cn"])
    address = _find_first(data, ["address", "location", "fulladdress"])
    area = _find_first(data, ["area", "square", "s"])
    category = _find_first(data, ["category", "landcategory"])
    usage = _find_first(data, ["permitteduse", "use", "utilization", "alloweduse", "vri"])
    cost = _find_first(data, ["cadastralcost", "cost", "cadastralvalue", "value"])
    obj_type = _find_first(data, ["type", "objecttype", "kind"])
    status = _find_first(data, ["status", "state"])

    lines = [f"КН: {cadnum}"]
    if found_cad and str(found_cad) != cadnum:
        lines.append(f"(В ответе сервисом найдено: {found_cad})")

    if obj_type:
        lines.append(f"Тип: {obj_type}")
    if status:
        lines.append(f"Статус: {status}")
    if address:
        lines.append(f"Адрес/местоположение: {address}")
    if area:
        lines.append(f"Площадь: {area}")
    if category:
        lines.append(f"Категория: {category}")
    if usage:
        lines.append(f"ВРИ/использование: {usage}")
    if cost:
        lines.append(f"Кад. стоимость: {cost}")

    # Если почти ничего не нашли — дадим короткий «сырой» фрагмент
    if len(lines) <= 1:
        raw = str(data)
        if len(raw) > 1400:
            raw = raw[:1400] + "…"
        lines.append("Не удалось уверенно выделить поля из ответа НСПД. Сырой ответ (обрезан):")
        lines.append(raw)

    return "\n".join(lines)


# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    clear_pending(context)
    clear_photo_stash(context)
    await update.message.reply_text("Выбери раздел:", reply_markup=kb_root())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_mode(context, "none")
    clear_pending(context)
    clear_photo_stash(context)
    await update.message.reply_text("Меню:", reply_markup=kb_root())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    data = q.data or ""

    # --- меню всегда
    if data == "nav:root":
        set_mode(context, "none")
        clear_pending(context)
        clear_photo_stash(context)
        await q.edit_message_text("Выбери раздел:", reply_markup=kb_root())
        return

    # --- ручной ввод координат/КН/СК
    if data == "manual:coords":
        context.user_data["awaiting_manual_input"] = "coords"
        await q.edit_message_text(
            "✍️ Ввод координат вручную.\n"
            "Пришли:\n"
            "X=72853345 Y=551668\n"
            "или несколько строк (в каждой строке 2 числа)."
        )
        return

    if data == "manual:cadnum":
        context.user_data["awaiting_manual_input"] = "cadnum"
        await q.edit_message_text(
            "✍️ Ввод кадастрового номера вручную.\n"
            "Пришли КН в формате:\n"
            "89:35:800113:31"
        )
        return

    if data == "manual:set_crs":
        context.user_data["awaiting_manual_input"] = "set_crs"
        src, dst = get_crs_pair(context)
        cur = f"Текущие СК: {src} -> {dst}\n\n" if src and dst else ""
        await q.edit_message_text(
            "⚙️ Задать системы координат.\n"
            f"{cur}"
            "Пришли строкой:\n"
            "СК: EPSG:3857 -> EPSG:4326\n"
            "или:\n"
            "СК: WGS84 -> WebMercator"
        )
        return

    # --- подтверждения
    if data.startswith("confirm:"):
        _, kind, action = data.split(":", 2)
        pending = context.user_data.get("pending")

        if not pending or pending.get("kind") != kind:
            await q.edit_message_text("Нет данных для подтверждения. Открой /menu")
            return

        if action == "ok":
            context.user_data["last_extracted"] = pending
            context.user_data.pop("pending", None)
            context.user_data.pop("awaiting_manual_input", None)

            if kind == "coords":
                x = pending.get("x")
                y = pending.get("y")
                await q.edit_message_text(
                    f"✅ Принято.\nX={x}\nY={y}\n\n"
                    "Теперь можно:\n"
                    "1) задать СК: «⚙️ Задать СК»\n"
                    "2) прислать новые координаты/список — я пересчитаю по заданным СК.",
                    reply_markup=kb_mode_actions_coords()
                )
            else:
                cad = pending.get("cadnum")
                await q.edit_message_text(
                    f"✅ Принято.\nКН: {cad}\n\n"
                    "Запрашиваю сведения…",
                    reply_markup=kb_mode_actions_cadnum()
                )
                # сразу тянем данные
                try:
                    data_json = await fetch_nspd_info(cad)
                    info = summarize_nspd_json(cad, data_json)
                    await q.message.reply_text(info, reply_markup=kb_land())
                except Exception as e:
                    logger.exception("NSPD fetch failed")
                    await q.message.reply_text(
                        "Не смог получить сведения (НСПД может быть недоступен/ограничивает запросы).\n"
                        f"Ошибка: {e}\n\n"
                        "Попробуй позже или пришли КН ещё раз.",
                        reply_markup=kb_land()
                    )
            return

        if action == "edit":
            context.user_data["awaiting_manual_input"] = kind
            await q.edit_message_text(
                "✏️ Ок. Пришли одним сообщением правильные данные:\n"
                "- для координат: `X=... Y=...` или несколько строк (в каждой 2 числа)\n"
                "- для кадастра: `89:xx:xxxxxx:xxx`"
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

    # --- маркшейдерия
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        clear_pending(context)
        src, dst = get_crs_pair(context)
        cur = f"Текущие СК: {src} -> {dst}\n\n" if src and dst else "СК не заданы.\n\n"
        await q.edit_message_text(
            "📐 Пересчёт координат.\n"
            f"{cur}"
            "1) Задай СК кнопкой «⚙️ Задать СК» (один раз)\n"
            "2) Пришли координаты (текстом или фото)\n\n"
            "Форматы:\n"
            "- X=... Y=...\n"
            "- или список строк (в каждой 2 числа)\n",
            reply_markup=kb_mode_actions_coords()
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

    # --- землеустройство
    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        clear_pending(context)
        await q.edit_message_text(
            "🏷️ Информация по кадастровому номеру.\n"
            "Пришли КН текстом или фото.\n"
            "Я распознаю и попрошу подтвердить — без 'додумывания'.",
            reply_markup=kb_mode_actions_cadnum()
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

    awaiting = context.user_data.get("awaiting_manual_input")

    # --- ввод СК
    if awaiting == "set_crs":
        src, dst = parse_crs_pair_from_text(text)
        if not src or not dst:
            await update.message.reply_text(
                "Не понял формат. Пришли так:\n"
                "СК: EPSG:3857 -> EPSG:4326\n"
                "или:\n"
                "СК: WGS84 -> WebMercator"
            )
            return

        # проверим что pyproj понимает
        try:
            _ = CRS.from_user_input(src)
            _ = CRS.from_user_input(dst)
        except Exception as e:
            await update.message.reply_text(
                "Не смог распознать одну из СК.\n"
                f"Ошибка: {e}\n\n"
                "Попробуй EPSG:4326 / EPSG:3857 или пришли точные EPSG."
            )
            return

        set_crs_pair(context, src, dst)
        context.user_data.pop("awaiting_manual_input", None)
        await update.message.reply_text(
            f"✅ СК заданы:\n{src} -> {dst}\n\nТеперь пришли координаты (текстом или фото).",
            reply_markup=kb_mode_actions_coords()
        )
        return

    # --- ручной ввод координат
    if awaiting == "coords":
        points = parse_points_from_text(text)
        if not points:
            await update.message.reply_text(
                "Не смог понять координаты.\n"
                "Пришли:\n"
                "X=72853345 Y=551668\n"
                "или несколько строк, в каждой 2 числа."
            )
            return

        # сохраняем pending
        if len(points) == 1:
            x, y = points[0]
            context.user_data["pending"] = {"kind": "coords", "x": x, "y": y, "points": points, "source": "manual"}
            context.user_data.pop("awaiting_manual_input", None)
            await update.message.reply_text(
                f"Я понял так:\nX={x}\nY={y}\nПодтверждаешь?",
                reply_markup=kb_confirm("coords")
            )
        else:
            context.user_data["pending"] = {"kind": "coords", "points": points, "source": "manual"}
            context.user_data.pop("awaiting_manual_input", None)
            await update.message.reply_text(
                f"Я понял список точек ({len(points)} шт.). Подтверждаешь?",
                reply_markup=kb_confirm("coords")
            )
        return

    # --- ручной ввод кадастра
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
                "Не вижу корректный КН (формат типа 89:35:800113:31). Пришли ещё раз."
            )
        return

    # --- фото пришло раньше без подписи
    last_photo_b64 = context.user_data.pop("last_photo_b64", None)
    if last_photo_b64:
        await update.message.reply_text("Принял подпись. Анализирую фото…")
        result_text, reply_markup = await process_photo_in_mode(context, mode, last_photo_b64, text)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

    # --- если режим не выбран
    if mode in ("none", "mine", "land"):
        await update.message.reply_text("Сначала выбери раздел/действие:", reply_markup=kb_root())
        return

    # --- нормативка (текстом через Claude)
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

    # --- кадастр текстом
    if mode == "land_cadnum":
        cadnums = parse_cadnums_from_text(text)
        if len(cadnums) == 1:
            cad = cadnums[0]
            context.user_data["pending"] = {"kind": "cadnum", "cadnum": cad, "source": "text"}
            await update.message.reply_text(
                f"Я распознал КН как:\n{cad}\nПодтверждаешь?",
                reply_markup=kb_confirm("cadnum")
            )
        else:
            await update.message.reply_text(
                "Не вижу корректный КН.\n"
                "Ожидаемый формат: 89:35:800113:31\n"
                "Можно нажать «✍️ Ввести КН вручную» или прислать фото.",
                reply_markup=kb_mode_actions_cadnum()
            )
        return

    # --- координаты текстом: если СК заданы — сразу пересчитываем
    if mode == "mine_coords":
        # 1) если человек прислал строку СК: ...
        src, dst = parse_crs_pair_from_text(text)
        if src and dst:
            try:
                _ = CRS.from_user_input(src)
                _ = CRS.from_user_input(dst)
                set_crs_pair(context, src, dst)
                await update.message.reply_text(
                    f"✅ СК заданы:\n{src} -> {dst}\n\nТеперь пришли координаты.",
                    reply_markup=kb_mode_actions_coords()
                )
            except Exception as e:
                await update.message.reply_text(
                    f"Не смог распознать СК.\nОшибка: {e}",
                    reply_markup=kb_mode_actions_coords()
                )
            return

        points = parse_points_from_text(text)
        if not points:
            await update.message.reply_text(
                "Не смог понять координаты.\n"
                "Либо пришли фото, либо нажми «✍️ Ввести координаты вручную».",
                reply_markup=kb_mode_actions_coords()
            )
            return

        src_crs, dst_crs = get_crs_pair(context)
        if not src_crs or not dst_crs:
            # сохраним как pending, но сначала попросим СК
            context.user_data["pending"] = {"kind": "coords", "points": points, "source": "text"}
            await update.message.reply_text(
                "Координаты принял, но СК не заданы.\n"
                "Задай СК (кнопка «⚙️ Задать СК») или пришли строкой:\n"
                "СК: EPSG:3857 -> EPSG:4326",
                reply_markup=kb_mode_actions_coords()
            )
            return

        # преобразование
        try:
            tr = build_transformer(src_crs, dst_crs)
            out_points: List[Tuple[float, float]] = []
            for x, y in points:
                xx, yy = tr.transform(x, y)
                out_points.append((xx, yy))

            await update.message.reply_text(
                "✅ Результат пересчёта:\n\n" + format_points_table(out_points),
                reply_markup=kb_mode_actions_coords()
            )
        except Exception as e:
            logger.exception("Transform error")
            await update.message.reply_text(
                "Не смог пересчитать. Возможные причины:\n"
                "- неверно задана СК\n"
                "- СК не совместимы\n"
                "- порядок координат не тот (lon/lat vs x/y)\n\n"
                f"Ошибка: {e}",
                reply_markup=kb_mode_actions_coords()
            )
        return

    # --- заглушка отчётов
    if mode == "mine_report":
        await update.message.reply_text(
            "Принял. Генератор отчётов ещё не подключён.\n"
            "Следующий шаг: подключим шаблоны и генерацию файлов.\n\n"
            f"Твой ввод:\n{text}",
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

    if caption:
        await update.message.reply_text("Фото с подписью получено. Анализирую…")
        result_text, reply_markup = await process_photo_in_mode(context, mode, image_b64, caption)
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

    if mode in ("mine_coords", "land_cadnum"):
        await update.message.reply_text("Фото получил. Пробую распознать…")
        result_text, reply_markup = await process_photo_in_mode(context, mode, image_b64, "")
        await update.message.reply_text(result_text, reply_markup=reply_markup)
        return

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
    cap_low = (caption or "").lower()

    # ====== Координаты (распознаём -> просим подтвердить) ======
    if mode == "mine_coords" or ("коорд" in cap_low) or ("x=" in cap_low) or ("y=" in cap_low):
        system_add = (
            "Задача: распознать координаты X и Y с изображения.\n"
            "КРИТИЧНО:\n"
            "- Не выдумывай и не 'додумывай' цифры.\n"
            "- Если цифра/символ плохо видна — поставь знак '?' на её месте.\n"
            "- Верни ровно в формате:\n"
            "TRANSCRIPTION:\n"
            "<перепиши как на бумаге, строка в строку>\n"
            "PARSED:\n"
            "X=<значение>\n"
            "Y=<значение>\n"
        )
        raw = ask_claude_with_image(caption.strip() or "Распознай X и Y.", image_b64, system_add=system_add)

        mx = re.search(r"\bX\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        my = re.search(r"\bY\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        x_s = mx.group(1).strip() if mx else ""
        y_s = my.group(1).strip() if my else ""

        x_val = _clean_num(x_s) if x_s and "?" not in x_s else None
        y_val = _clean_num(y_s) if y_s and "?" not in y_s else None

        context.user_data["pending"] = {
            "kind": "coords",
            "x": x_val,
            "y": y_val,
            "points": [(x_val, y_val)] if x_val is not None and y_val is not None else [],
            "raw": raw,
            "source": "photo",
        }

        msg = (
            "Я распознал координаты так (проверь внимательно):\n\n"
            f"{raw}\n\n"
            "✅ Подтвердить / ✏️ Исправить вручную"
        )
        return msg, kb_confirm("coords")

    # ====== Кадастровый номер (распознаём -> просим подтвердить) ======
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
        raw = ask_claude_with_image(caption.strip() or "Распознай кадастровый номер.", image_b64, system_add=system_add)
        mc = re.search(r"\bCADNUM\s*=\s*([0-9?:]+)", raw, re.IGNORECASE)
        cad_guess = mc.group(1).strip() if mc else ""

        cad = None
        cadnums = parse_cadnums_from_text(cad_guess) if cad_guess and "?" not in cad_guess else []
        if len(cadnums) == 1:
            cad = cadnums[0]

        context.user_data["pending"] = {"kind": "cadnum", "cadnum": cad, "raw": raw, "source": "photo"}
        msg = (
            "Я распознал КН так (проверь внимательно):\n\n"
            f"{raw}\n\n"
            "✅ Подтвердить / ✏️ Исправить вручную"
        )
        return msg, kb_confirm("cadnum")

    return ("Не понял, что извлекать с фото. Открой /menu и выбери режим.", kb_root())


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
