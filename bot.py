import os
import re
import csv
import base64
import logging
from io import BytesIO, StringIO
from typing import Optional, Tuple, List, Dict, Any

import httpx
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

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
    "Ты — помощник для маркшейдеров и специалистов по землеустройству.\n"
    "КРИТИЧНО: при распознавании с фото не выдумывай и не додумывай цифры.\n"
    "Если цифра/символ неразборчивы — ставь '?' в этом месте.\n"
)

HELP_TEXT = (
    "Команды:\n"
    "/start — меню\n"
    "/menu — меню\n"
    "/help — помощь\n\n"
    "Пересчёт координат: выбираешь исходную/конечную СК и формат вывода, "
    "потом присылаешь координаты (текст/фото/файл txt/csv).\n"
    "Кадастр: присылай КН текстом/фото/файлом.\n"
)


# ================== REGEX ==================
CADNUM_RE = re.compile(r"\b\d{2}:\d{2}:\d{6,7}:\d+\b")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


# ================== DMS HELPERS ==================
# Поддерживаемые форматы ГМС:
#   77 05 28  /  77 05 28.5
#   77°05'28"  /  77°05'28.5"
#   77-05-28
#   77d05m28s
DMS_LINE_RE = re.compile(
    r"([-+]?\d+)[°\-d\s]+"   # градусы
    r"(\d+)['\-m\s]+"         # минуты
    r"([\d.]+)[\"s]?"         # секунды
    r"\s*"
    r"([NSEWnsew])?"          # опциональная буква стороны света
)

def dms_to_dd(deg: str, mn: str, sec: str, hemi: str = "") -> float:
    d = float(deg)
    m = float(mn)
    s = float(sec)
    dd = abs(d) + m / 60.0 + s / 3600.0
    if d < 0 or hemi.upper() in ("S", "W"):
        dd = -dd
    return dd

def parse_dms_line(line: str) -> Optional[Tuple[float, float]]:
    """Парсит строку с двумя ГМС-координатами. Возвращает (x, y) или None."""
    matches = DMS_LINE_RE.findall(line)
    if len(matches) >= 2:
        x = dms_to_dd(*matches[0])
        y = dms_to_dd(*matches[1])
        return (x, y)
    return None

def parse_points_auto(text: str) -> List[Tuple[float, float]]:
    """Автоопределение формата: сначала пробуем ГМС, затем десятичные."""
    pts: List[Tuple[float, float]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Проверяем, похоже ли на ГМС (есть °, ', ", d, m, s или 3+ числа)
        nums_in_line = NUM_RE.findall(line)
        has_dms_marker = any(c in line for c in "°'\"dms")
        if has_dms_marker or len(nums_in_line) >= 3:
            pt = parse_dms_line(line)
            if pt:
                pts.append(pt)
                continue
        # Иначе — десятичные
        if len(nums_in_line) >= 2:
            x = _clean_num(nums_in_line[0])
            y = _clean_num(nums_in_line[1])
            if x is not None and y is not None:
                pts.append((x, y))
    # Если построчно ничего не нашли — пробуем весь текст как одну строку
    if not pts:
        pt = parse_dms_line(text)
        if pt:
            pts.append(pt)
        else:
            nums = NUM_RE.findall(text or "")
            if len(nums) >= 2:
                x = _clean_num(nums[0])
                y = _clean_num(nums[1])
                if x is not None and y is not None:
                    pts.append((x, y))
    return pts


# ================== CRS OPTIONS (SHORT ASCII IDS ONLY!) ==================
# callback_data must be <= 64 bytes
CRS_OPTIONS: Dict[str, Dict[str, str]] = {
    "wgs84": {
        "label": "WGS84 (географические)",
        "kind": "epsg",
        "code": "EPSG:4326",
    },
    "merc": {
        "label": "WebMercator (EPSG:3857)",
        "kind": "epsg",
        "code": "EPSG:3857",
    },
    "sk42gk": {
        "label": "СК-42 (Гаусс-Крюгер, выбрать зону)",
        "kind": "sk42_zone",
        "code": "",
    },
}

OUTPUT_PRESETS = {
    "Показать в чате": "chat",
    "Сгенерировать файл (CSV)": "csv",
}


# ================== CALLBACK DATA VALIDATOR ==================
def _assert_cb(cb: str) -> str:
    b = cb.encode("utf-8")
    if len(b) > 64:
        logger.error(f"callback_data too long ({len(b)} bytes): {cb!r}")
        return "cb:too_long"
    return cb


# ================== SAFE ANSWER / SAFE EDIT ==================
async def safe_answer(q) -> None:
    try:
        await q.answer()
    except Exception:
        pass


async def safe_edit(q, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, parse_mode: Optional[str] = None) -> None:
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        logger.warning(f"safe_edit BadRequest: {e}")
        try:
            await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e2:
            logger.warning(f"safe_edit fallback failed: {e2}")
    except Exception as e:
        logger.warning(f"safe_edit error: {e}")
        try:
            await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e2:
            logger.warning(f"safe_edit fallback failed: {e2}")


# ================== UI HELPERS ==================
def kb_nav(back_to: Optional[str], include_menu: bool = True) -> List[List[InlineKeyboardButton]]:
    row: List[InlineKeyboardButton] = []
    if back_to:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=_assert_cb(back_to)))
    if include_menu:
        row.append(InlineKeyboardButton("🏠 Меню", callback_data=_assert_cb("nav:root")))
    return [row] if row else []


def kb_root() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏗️ Маркшейдерия", callback_data=_assert_cb("root:mine"))],
        [InlineKeyboardButton("🗺️ Землеустройство", callback_data=_assert_cb("root:land"))],
    ]
    return InlineKeyboardMarkup(rows)


def kb_mine() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📐 Пересчёт координат", callback_data=_assert_cb("mine:coords"))],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data=_assert_cb("mine:norms"))],
        [InlineKeyboardButton("🧾 Составление отчёта", callback_data=_assert_cb("mine:report"))],
    ]
    rows += kb_nav(back_to="nav:root", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_land() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏷️ Инфо по кадастровому номеру", callback_data=_assert_cb("land:cadnum"))],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data=_assert_cb("land:norms"))],
    ]
    rows += kb_nav(back_to="nav:root", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_main(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    src = context.user_data.get("coords_src_label", "не выбрана")
    dst = context.user_data.get("coords_dst_label", "не выбрана")
    out = context.user_data.get("coords_out_mode", "не выбран")

    rows = [
        [InlineKeyboardButton(f"1) Исходная СК: {src}", callback_data=_assert_cb("coords:set_src"))],
        [InlineKeyboardButton(f"2) Конечная СК: {dst}", callback_data=_assert_cb("coords:set_dst"))],
        [InlineKeyboardButton(f"3) Вывод: {out}", callback_data=_assert_cb("coords:set_out"))],
        [InlineKeyboardButton("✅ Готово: прислать координаты", callback_data=_assert_cb("coords:ready"))],
    ]
    rows += kb_nav(back_to="nav:mine", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_crs(kind: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for crs_id, meta in CRS_OPTIONS.items():
        cb = _assert_cb(f"coords:pick:{kind}:{crs_id}")
        rows.append([InlineKeyboardButton(meta["label"], callback_data=cb)])
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_zone(kind: str, page: str = "1") -> InlineKeyboardMarkup:
    start = 1 if page == "1" else 31
    end = 30 if page == "1" else 60

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for z in range(start, end + 1):
        cb = _assert_cb(f"coords:zone:{kind}:{z}")
        row.append(InlineKeyboardButton(str(z), callback_data=cb))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if page == "1":
        rows.append([InlineKeyboardButton("➡️ 31–60", callback_data=_assert_cb("coords:zone_page:2"))])
    else:
        rows.append([InlineKeyboardButton("⬅️ 1–30", callback_data=_assert_cb("coords:zone_page:1"))])

    back = "coords:set_src" if kind == "src" else "coords:set_dst"
    rows += kb_nav(back_to=back, include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_output() -> InlineKeyboardMarkup:
    rows = []
    for label, mode in OUTPUT_PRESETS.items():
        rows.append([InlineKeyboardButton(label, callback_data=_assert_cb(f"coords:out:{mode}"))])
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_ready() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✍️ Ввести координаты вручную", callback_data=_assert_cb("coords:manual"))],
        [InlineKeyboardButton("📷 Прислать фото координат", callback_data=_assert_cb("coords:photo_help"))],
        [InlineKeyboardButton("📎 Прислать файл (txt/csv)", callback_data=_assert_cb("coords:file_help"))],
        [InlineKeyboardButton("🔁 Сменить настройки СК/вывода", callback_data=_assert_cb("coords:home"))],
    ]
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_land_cadnum() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ Ввести КН вручную", callback_data=_assert_cb("cad:manual"))],
        [InlineKeyboardButton("📷 Прислать фото КН", callback_data=_assert_cb("cad:photo_help"))],
        [InlineKeyboardButton("📎 Прислать файл (txt/csv) с КН", callback_data=_assert_cb("cad:file_help"))],
    ]
    rows += kb_nav(back_to="nav:land", include_menu=True)
    return InlineKeyboardMarkup(rows)


# ================== STATE ==================
def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode


def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "none")


def reset_coords_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in [
        "coords_src", "coords_dst", "coords_src_label", "coords_dst_label",
        "coords_out_mode", "coords_out_mode_code", "coords_zone_page",
        "awaiting_zone_kind", "awaiting",
    ]:
        context.user_data.pop(k, None)


# ================== COORD TRANSFORM ==================
def _clean_num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


def parse_points_from_text(text: str) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for line in (text or "").splitlines():
        nums = NUM_RE.findall(line)
        if len(nums) >= 2:
            x = _clean_num(nums[0])
            y = _clean_num(nums[1])
            if x is not None and y is not None:
                pts.append((x, y))
    if not pts:
        nums = NUM_RE.findall(text or "")
        if len(nums) >= 2:
            x = _clean_num(nums[0])
            y = _clean_num(nums[1])
            if x is not None and y is not None:
                pts.append((x, y))
    return pts


def transform_points(points: List[Tuple[float, float]], src_code: str, dst_code: str) -> List[Tuple[float, float]]:
    crs_src = CRS.from_user_input(src_code)
    crs_dst = CRS.from_user_input(dst_code)
    tr = Transformer.from_crs(crs_src, crs_dst, always_xy=True)
    return [tr.transform(x, y) for x, y in points]


def format_points_table(points: List[Tuple[float, float]]) -> str:
    lines = ["N;X;Y"]
    for i, (x, y) in enumerate(points, start=1):
        lines.append(f"{i};{x:.6f};{y:.6f}")
    return "\n".join(lines)


def make_csv_bytes(points: List[Tuple[float, float]]) -> bytes:
    sio = StringIO()
    w = csv.writer(sio, delimiter=";")
    w.writerow(["N", "X", "Y"])
    for i, (x, y) in enumerate(points, start=1):
        w.writerow([i, f"{x:.6f}", f"{y:.6f}"])
    return sio.getvalue().encode("utf-8-sig")


# ================== CADASTRE ==================
async def fetch_cadaster_info(cadnum: str) -> Dict[str, Any]:
    """
    Запрашивает сведения через публичный API ПКК (pkk.rosreestr.ru).
    Возвращает dict с полями объекта или пустой dict если не найден.
    """
    # Поиск объекта по КН
    search_url = "https://pkk.rosreestr.ru/api/features/1"
    params = {
        "text": cadnum,
        "limit": "1",
        "skip": "0",
        "inPoint": "false",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://pkk.rosreestr.ru/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://pkk.rosreestr.ru",
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as c:
        r = await c.get(search_url, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()

    features = data.get("features") or []
    if not features:
        return {}

    feature = features[0]
    attrs = feature.get("attrs") or {}

    # Пытаемся получить детальную карточку
    cn = attrs.get("cn") or cadnum
    detail_url = f"https://pkk.rosreestr.ru/api/features/1/{cn.replace(':', '%3A')}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as c:
            rd = await c.get(detail_url, headers=headers)
            if rd.status_code == 200:
                detail = rd.json()
                attrs = (detail.get("feature") or {}).get("attrs") or attrs
    except Exception:
        pass

    return attrs


def format_cadaster_attrs(attrs: Dict[str, Any], cadnum: str) -> str:
    """Форматирует атрибуты в читаемый текст."""
    if not attrs:
        return f"По КН {cadnum} сведения не найдены в открытых источниках."

    lines = ["📋 Сведения по КН: " + cadnum, ""]

    field_map = [
        ("cn",          "Кадастровый номер"),
        ("address",     "Адрес"),
        ("area_value",  "Площадь"),
        ("area_unit",   "Единица площади"),
        ("category_type", "Категория земель"),
        ("util_by_doc", "Разрешённое использование"),
        ("util_code",   "Код использования"),
        ("land_record_type", "Тип объекта"),
        ("statecd",     "Статус"),
        ("rifr",        "Кадастровая стоимость"),
        ("reestr_date", "Дата постановки на учёт"),
        ("cad_unit",    "Кадастровый округ"),
        ("region_name", "Регион"),
    ]

    for key, label in field_map:
        val = attrs.get(key)
        if val not in (None, "", 0):
            lines.append(f"• {label}: {val}")

    lines.append("\n🔗 Источник: pkk.rosreestr.ru")
    return "\n".join(lines)


def parse_cadnums_from_text(text: str) -> List[str]:
    return sorted(set(CADNUM_RE.findall(text or "")))


# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_coords_wizard(context)
    set_mode(context, "none")
    await update.message.reply_text("Выбери раздел:", reply_markup=kb_root())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_coords_wizard(context)
    set_mode(context, "none")
    await update.message.reply_text("Меню:", reply_markup=kb_root())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    data = q.data or ""

    # ── global nav ──
    if data == "nav:root":
        reset_coords_wizard(context)
        set_mode(context, "none")
        await safe_edit(q, "Выбери раздел:", reply_markup=kb_root())
        return

    if data == "nav:mine":
        set_mode(context, "mine")
        await safe_edit(q, "Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "nav:land":
        set_mode(context, "land")
        await safe_edit(q, "Землеустройство:", reply_markup=kb_land())
        return

    # ── root sections ──
    if data == "root:mine":
        set_mode(context, "mine")
        await safe_edit(q, "Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        await safe_edit(q, "Землеустройство:", reply_markup=kb_land())
        return

    # ── mine submenu ──
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        await safe_edit(
            q,
            "📐 Пересчёт координат — настройки.\n"
            "Сначала выбери исходную/конечную СК и формат вывода.",
            reply_markup=kb_coords_main(context),
        )
        return

    if data == "mine:norms":
        await safe_edit(
            q,
            "📚 Нормативная документация (маркшейдерия) — раздел в разработке.",
            reply_markup=InlineKeyboardMarkup(kb_nav("nav:mine")),
        )
        return

    if data == "mine:report":
        await safe_edit(
            q,
            "🧾 Составление отчёта — раздел в разработке.",
            reply_markup=InlineKeyboardMarkup(kb_nav("nav:mine")),
        )
        return

    # ── land submenu ──
    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        await safe_edit(q, "🏷️ Кадастровые сведения — выбери способ ввода:", reply_markup=kb_land_cadnum())
        return

    if data == "land:norms":
        await safe_edit(
            q,
            "📚 Нормативная документация (землеустройство) — раздел в разработке.",
            reply_markup=InlineKeyboardMarkup(kb_nav("nav:land")),
        )
        return

    # ── coords wizard ──
    if data == "coords:home":
        set_mode(context, "mine_coords")
        await safe_edit(
            q,
            "📐 Пересчёт координат — настройки.",
            reply_markup=kb_coords_main(context),
        )
        return

    if data == "coords:set_src":
        await safe_edit(q, "Выбери ИСХОДНУЮ систему координат:", reply_markup=kb_coords_pick_crs("src"))
        return

    if data == "coords:set_dst":
        await safe_edit(q, "Выбери КОНЕЧНУЮ систему координат:", reply_markup=kb_coords_pick_crs("dst"))
        return

    if data.startswith("coords:pick:"):
        # coords:pick:src:wgs84
        parts = data.split(":")
        if len(parts) != 4:
            await safe_edit(q, "Не понял выбор.", reply_markup=kb_coords_main(context))
            return

        kind = parts[2]   # src / dst
        crs_id = parts[3]
        meta = CRS_OPTIONS.get(crs_id)
        if not meta:
            await safe_edit(q, "Неизвестная СК.", reply_markup=kb_coords_main(context))
            return

        if meta["kind"] == "epsg":
            code = meta["code"]
            label = meta["label"]
            if kind == "src":
                context.user_data["coords_src"] = code
                context.user_data["coords_src_label"] = label
            else:
                context.user_data["coords_dst"] = code
                context.user_data["coords_dst_label"] = label
            await safe_edit(q, "✅ Сохранено.", reply_markup=kb_coords_main(context))
            return

        if meta["kind"] == "sk42_zone":
            context.user_data["coords_zone_page"] = "1"
            context.user_data["awaiting_zone_kind"] = kind
            await safe_edit(
                q,
                "Выбери зону СК-42 (Гаусс-Крюгер):",
                reply_markup=kb_coords_pick_zone(kind, "1"),
            )
            return

    if data.startswith("coords:zone_page:"):
        page = data.split(":")[-1]
        page = page if page in ("1", "2") else "1"
        context.user_data["coords_zone_page"] = page
        kind = context.user_data.get("awaiting_zone_kind", "src")
        await safe_edit(
            q,
            "Выбери зону СК-42 (Гаусс-Крюгер):",
            reply_markup=kb_coords_pick_zone(kind, page),
        )
        return

    if data.startswith("coords:zone:"):
        # coords:zone:src:42
        parts = data.split(":")
        if len(parts) != 4:
            await safe_edit(q, "Не понял выбор зоны.", reply_markup=kb_coords_main(context))
            return

        kind = parts[2]
        z = int(parts[3])
        if z < 1 or z > 60:
            await safe_edit(q, "Зона должна быть 1..60.", reply_markup=kb_coords_main(context))
            return

        epsg = f"EPSG:{28400 + z}"
        label = f"СК-42 ГК зона {z}"
        if kind == "src":
            context.user_data["coords_src"] = epsg
            context.user_data["coords_src_label"] = label
        else:
            context.user_data["coords_dst"] = epsg
            context.user_data["coords_dst_label"] = label

        await safe_edit(q, f"✅ Зона {z} сохранена.", reply_markup=kb_coords_main(context))
        return

    if data == "coords:set_out":
        await safe_edit(q, "Выбери формат вывода:", reply_markup=kb_coords_pick_output())
        return

    if data.startswith("coords:out:"):
        mode = data.split(":")[-1]
        if mode not in ("chat", "csv"):
            await safe_edit(q, "Не понял формат вывода.", reply_markup=kb_coords_main(context))
            return
        context.user_data["coords_out_mode"] = "Показать в чате" if mode == "chat" else "Файл CSV"
        context.user_data["coords_out_mode_code"] = mode
        await safe_edit(q, "✅ Формат вывода сохранён.", reply_markup=kb_coords_main(context))
        return

    if data == "coords:ready":
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")
        if not src or not dst or not out_mode:
            await safe_edit(
                q,
                "⚠️ Нужно выбрать исходную СК, конечную СК и формат вывода.",
                reply_markup=kb_coords_main(context),
            )
            return
        context.user_data["awaiting"] = "coords_input"
        await safe_edit(
            q,
            "✅ Настройки готовы. Выбери способ ввода координат.\n\n"
            "Поддерживаются форматы:\n"
            "• Десятичные: <pre>77.091111 63.228889</pre>\n"
            "• ГМС: <pre>77 05 28  63 13 44</pre>\n"
            "• Метры: <pre>72853345 551668</pre>",
            reply_markup=kb_coords_ready(),
            parse_mode="HTML",
        )
        return

    if data == "coords:manual":
        context.user_data["awaiting"] = "coords_manual"
        await safe_edit(
            q,
            "✍️ Пришли координаты — каждая точка на отдельной строке.\n\n"
            "Поддерживаемые форматы:\n\n"
            "Десятичные градусы:\n"
            "<pre>77.091111 63.228889</pre>\n\n"
            "Градусы минуты секунды (ГМС):\n"
            "<pre>77 05 28  63 13 44</pre>\n"
            "<pre>77°05'28\" 63°13'44\"</pre>\n\n"
            "Прямоугольные (метры):\n"
            "<pre>72853345 551668</pre>\n\n"
            "Несколько точек — каждая с новой строки.",
            reply_markup=kb_coords_ready(),
            parse_mode="HTML",
        )
        return

    if data == "coords:file_help":
        context.user_data["awaiting"] = "coords_file"
        await safe_edit(q, "📎 Пришли файл .txt/.csv с координатами (X Y на строку).", reply_markup=kb_coords_ready())
        return

    if data == "coords:photo_help":
        context.user_data["awaiting"] = "coords_photo"
        await safe_edit(q, "📷 Пришли фото с координатами.", reply_markup=kb_coords_ready())
        return

    # ── cadastre ──
    if data == "cad:manual":
        set_mode(context, "cad_manual")
        context.user_data["awaiting"] = "cad_manual"
        await safe_edit(
            q,
            "✅ Введи кадастровый номер.\nФормат: NN:NN:NNNNNN:N\nПример: 89:35:800113:31",
            reply_markup=InlineKeyboardMarkup(kb_nav("land:cadnum")),
        )
        return

    if data == "cad:photo_help":
        context.user_data["awaiting"] = "cad_photo"
        await safe_edit(
            q,
            "📷 Пришли фото с кадастровым номером.",
            reply_markup=InlineKeyboardMarkup(kb_nav("land:cadnum")),
        )
        return

    if data == "cad:file_help":
        context.user_data["awaiting"] = "cad_file"
        await safe_edit(
            q,
            "📎 Пришли файл .txt/.csv со списком кадастровых номеров (по одному на строку).",
            reply_markup=InlineKeyboardMarkup(kb_nav("land:cadnum")),
        )
        return

    # ── fallback ──
    await safe_edit(q, "Не понял команду. Нажми /menu", reply_markup=kb_root())


# ================== MESSAGE HANDLERS ==================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    text = update.message.text or ""

    if awaiting in ("coords_input", "coords_manual"):
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")
        if not src or not dst or not out_mode:
            await update.message.reply_text(
                "⚠️ Сначала выбери настройки (СК и формат вывода). Нажми /menu",
                reply_markup=kb_root(),
            )
            return
        pts = parse_points_auto(text)
        if not pts:
            await update.message.reply_text(
                "Не вижу координат. Пример формата:\n72853345 551668\n72853400 551700"
            )
            return
        await do_transform_and_respond(update, context, pts)
        return

    if awaiting == "cad_manual":
        cadnums = parse_cadnums_from_text(text)
        if not cadnums:
            await update.message.reply_text(
                "Не вижу кадастрового номера. Пример: 89:35:800113:31"
            )
            return
        cad = cadnums[0]
        await update.message.reply_text(f"🔍 Запрашиваю сведения по КН: {cad} …")
        try:
            attrs = await fetch_cadaster_info(cad)
            text_out = format_cadaster_attrs(attrs, cad)
            await update.message.reply_text(text_out)
        except Exception as e:
            await update.message.reply_text(f"Не смог получить сведения: {e}")
        return

    await update.message.reply_text("Открой /menu", reply_markup=kb_root())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    if awaiting != "coords_file":
        await update.message.reply_text("Сейчас не жду файл. Открой /menu", reply_markup=kb_root())
        return

    src = context.user_data.get("coords_src")
    dst = context.user_data.get("coords_dst")
    out_mode = context.user_data.get("coords_out_mode_code")
    if not src or not dst or not out_mode:
        await update.message.reply_text("⚠️ Сначала выбери настройки. /menu")
        return

    doc = update.message.document
    if doc.file_size > 2 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой (макс. 2 МБ).")
        return

    file = await doc.get_file()
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    try:
        text = bio.read().decode("utf-8-sig")
    except Exception:
        await update.message.reply_text("Не смог прочитать файл. Пришли UTF-8 txt/csv.")
        return

    pts = parse_points_from_text(text)
    if not pts:
        await update.message.reply_text("Не нашёл координат в файле. Формат: X Y на строку.")
        return

    await do_transform_and_respond(update, context, pts, filename_hint=doc.file_name or "coords")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    if awaiting not in ("coords_photo", "cad_photo"):
        await update.message.reply_text("Сейчас не жду фото. Открой /menu", reply_markup=kb_root())
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    img_b64 = base64.b64encode(bio.read()).decode()

    if awaiting == "coords_photo":
        prompt = (
            "На фото координаты. Распознай все числовые пары (X Y) построчно. "
            "Если символ неразборчив — ставь '?'. НЕ додумывай цифры. "
            "Верни только строки вида X Y, по одной на строку."
        )
    else:
        prompt = (
            "На фото кадастровый номер. Распознай его точно. "
            "Если символ неразборчив — ставь '?'. НЕ додумывай цифры. "
            "Верни только распознанную строку."
        )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT_BASE,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        recognized = resp.content[0].text.strip()
    except Exception as e:
        await update.message.reply_text(f"Ошибка распознавания: {e}")
        return

    if awaiting == "coords_photo":
        has_doubt = "?" in recognized
        await update.message.reply_text(
            f"Я распознал:\n{recognized}\n\n"
            + ("⚠️ Есть сомнительные символы ('?'). Проверь и пришли более чёткое фото или введи вручную." if has_doubt else "✅ Проверь и подтверди — или введи координаты вручную если что-то не так.")
        )
        pts = parse_points_from_text(recognized)
        if pts and not has_doubt:
            src = context.user_data.get("coords_src")
            dst = context.user_data.get("coords_dst")
            out_mode = context.user_data.get("coords_out_mode_code")
            if src and dst and out_mode:
                await do_transform_and_respond(update, context, pts)
    else:
        has_doubt = "?" in recognized
        await update.message.reply_text(
            f"Я распознал: {recognized}\n\n"
            + ("⚠️ Есть сомнительные символы. Проверь или введи вручную." if has_doubt else "✅ Проверь номер. Если верно — введи его текстом для запроса сведений.")
        )
        if not has_doubt:
            cadnums = parse_cadnums_from_text(recognized)
            if cadnums:
                context.user_data["awaiting"] = "cad_manual"


# ================== TRANSFORM + RESPOND ==================
async def do_transform_and_respond(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    points: List[Tuple[float, float]],
    filename_hint: str = "coords",
) -> None:
    src = context.user_data.get("coords_src")
    dst = context.user_data.get("coords_dst")
    out_mode = context.user_data.get("coords_out_mode_code")

    try:
        out_points = transform_points(points, src, dst)
    except Exception as e:
        logger.exception("Transform error")
        await update.message.reply_text(f"❌ Ошибка пересчёта: {e}")
        return

    if out_mode == "chat":
        table = format_points_table(out_points)
        msg = f"✅ Результат ({len(out_points)} точек):\n\n<pre>{table}</pre>"
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb_coords_ready())
        return

    csv_bytes = make_csv_bytes(out_points)
    bio = BytesIO(csv_bytes)
    safe_name = re.sub(r"[^\w\-.]", "_", filename_hint)
    bio.name = f"{safe_name}_converted.csv"
    bio.seek(0)
    await update.message.reply_document(
        document=InputFile(bio),
        filename=bio.name,
        caption=f"✅ Готово. {len(out_points)} точек. CSV (разделитель ';').",
        reply_markup=kb_coords_ready(),
    )


# ================== ERROR HANDLER ==================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Произошёл временный сбой. Повтори действие.")
    except Exception:
        pass


# ================== MAIN ==================
def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_error_handler(error_handler)

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
