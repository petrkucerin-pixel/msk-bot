import os
import re
import csv
import base64
import logging
from io import BytesIO, StringIO
from dataclasses import dataclass
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


# ================== CLAUDE (for photo reading) ==================
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


# ================== CRS PRESETS ==================
# Важно:
# - WGS84 географические: EPSG:4326 (lon, lat)
# - WebMercator: EPSG:3857
# - СК-42 (Пулково 1942) Гаусс-Крюгер зоны: EPSG:28401..28460 (зона 1..60)
#
# Замечание: "СК-42 прямоугольные" = как правило GK в нужной зоне.
#
CRS_PRESETS = {
    "WGS84 (географические)": {"kind": "epsg", "code": "EPSG:4326"},
    "WebMercator (EPSG:3857)": {"kind": "epsg", "code": "EPSG:3857"},
    "СК-42 (Гаусс-Крюгер, выбрать зону)": {"kind": "sk42_zone"},
}

OUTPUT_PRESETS = {
    "Показать в чате": "chat",
    "Сгенерировать файл (CSV)": "csv",
}


# ================== UI HELPERS ==================
def kb_nav(back_to: Optional[str], include_menu: bool = True) -> List[List[InlineKeyboardButton]]:
    row: List[InlineKeyboardButton] = []
    if back_to:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=back_to))
    if include_menu:
        row.append(InlineKeyboardButton("🏠 Меню", callback_data="nav:root"))
    return [row] if row else []


def kb_root() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏗️ Маркшейдерия", callback_data="root:mine")],
        [InlineKeyboardButton("🗺️ Землеустройство", callback_data="root:land")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_mine() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📐 Пересчёт координат", callback_data="mine:coords")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="mine:norms")],
        [InlineKeyboardButton("🧾 Составление отчёта", callback_data="mine:report")],
    ]
    rows += kb_nav(back_to="nav:root", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_land() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏷️ Инфо по кадастровому номеру", callback_data="land:cadnum")],
        [InlineKeyboardButton("📚 Нормативная документация", callback_data="land:norms")],
    ]
    rows += kb_nav(back_to="nav:root", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_main() -> InlineKeyboardMarkup:
    # Мастер-меню координат
    src = _get(context_key="coords_src_label", default="не выбрана")
    dst = _get(context_key="coords_dst_label", default="не выбрана")
    out = _get(context_key="coords_out_mode", default="не выбран")

    rows = [
        [InlineKeyboardButton(f"1) Исходная СК: {src}", callback_data="coords:set_src")],
        [InlineKeyboardButton(f"2) Конечная СК: {dst}", callback_data="coords:set_dst")],
        [InlineKeyboardButton(f"3) Вывод: {out}", callback_data="coords:set_out")],
        [InlineKeyboardButton("✅ Готово: прислать координаты", callback_data="coords:ready")],
    ]
    rows += kb_nav(back_to="nav:mine", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_crs(kind: str) -> InlineKeyboardMarkup:
    # kind = "src" or "dst"
    rows: List[List[InlineKeyboardButton]] = []
    for label in CRS_PRESETS.keys():
        rows.append([InlineKeyboardButton(label, callback_data=f"coords:pick_{kind}:{label}")])
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_zone(kind: str) -> InlineKeyboardMarkup:
    # Показываем зоны 1..30 и 31..60 переключалками
    # Чтобы не городить пагинацию - две страницы
    page = _get("coords_zone_page", "1")
    page = page if page in ("1", "2") else "1"
    start = 1 if page == "1" else 31
    end = 30 if page == "1" else 60

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for z in range(start, end + 1):
        row.append(InlineKeyboardButton(str(z), callback_data=f"coords:zone_{kind}:{z}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    switch_row = []
    if page == "1":
        switch_row.append(InlineKeyboardButton("➡️ 31–60", callback_data="coords:zone_page:2"))
    else:
        switch_row.append(InlineKeyboardButton("⬅️ 1–30", callback_data="coords:zone_page:1"))
    rows.append(switch_row)

    rows += kb_nav(back_to="coords:set_src" if kind == "src" else "coords:set_dst", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_output() -> InlineKeyboardMarkup:
    rows = []
    for label, mode in OUTPUT_PRESETS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"coords:out:{mode}")])
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_land_cadnum() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ Ввести КН вручную", callback_data="cad:manual")],
        [InlineKeyboardButton("📷 Прислать фото КН", callback_data="cad:photo_help")],
        [InlineKeyboardButton("📎 Прислать файл (txt/csv) с КН", callback_data="cad:file_help")],
    ]
    rows += kb_nav(back_to="nav:land", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_ready() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✍️ Ввести координаты вручную", callback_data="coords:manual")],
        [InlineKeyboardButton("📷 Прислать фото координат", callback_data="coords:photo_help")],
        [InlineKeyboardButton("📎 Прислать файл (txt/csv) с координатами", callback_data="coords:file_help")],
        [InlineKeyboardButton("🔁 Сменить настройки СК/вывода", callback_data="coords:home")],
    ]
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


# ================== small context helper ==================
# (чтобы kb_coords_main мог брать значения даже когда context не передан)
_GLOBAL_CTX: Dict[str, Any] = {}


def _set(context_key: str, value: Any) -> None:
    _GLOBAL_CTX[context_key] = value


def _get(context_key: str, default: Any = None) -> Any:
    return _GLOBAL_CTX.get(context_key, default)


# ================== STATE ==================
def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode


def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "none")


def reset_coords_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("coords_src", None)
    context.user_data.pop("coords_dst", None)
    context.user_data.pop("coords_src_label", None)
    context.user_data.pop("coords_dst_label", None)
    context.user_data.pop("coords_out_mode", None)
    context.user_data.pop("coords_zone_page", None)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("last_photo_b64", None)


def sync_globals_from_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    # чтобы kb_coords_main мог показать значения
    _set("coords_src_label", context.user_data.get("coords_src_label", "не выбрана"))
    _set("coords_dst_label", context.user_data.get("coords_dst_label", "не выбрана"))
    _set("coords_out_mode", context.user_data.get("coords_out_mode", "не выбран"))
    _set("coords_zone_page", context.user_data.get("coords_zone_page", "1"))


# ================== CLAUDE (photo) ==================
def ask_claude_with_image(prompt_text: str, image_b64: str, system_add: str) -> str:
    prompt_text = (prompt_text or "").strip() or "Распознай данные."
    system = SYSTEM_PROMPT_BASE + "\n" + (system_add or "")

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
    return ("\n".join(out)).strip()


# ================== COORD PARSING ==================
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
    # если одной строкой через пробел
    if not pts:
        nums = NUM_RE.findall(text or "")
        if len(nums) >= 2:
            x = _clean_num(nums[0])
            y = _clean_num(nums[1])
            if x is not None and y is not None:
                pts.append((x, y))
    return pts


def format_points_table(points: List[Tuple[float, float]]) -> str:
    lines = ["N;X;Y"]
    for i, (x, y) in enumerate(points, start=1):
        lines.append(f"{i};{x};{y}")
    return "\n".join(lines)


def transformer_from_user_codes(src_code: str, dst_code: str) -> Transformer:
    crs_src = CRS.from_user_input(src_code)
    crs_dst = CRS.from_user_input(dst_code)
    return Transformer.from_crs(crs_src, crs_dst, always_xy=True)


def transform_points(points: List[Tuple[float, float]], src_code: str, dst_code: str) -> List[Tuple[float, float]]:
    tr = transformer_from_user_codes(src_code, dst_code)
    out: List[Tuple[float, float]] = []
    for x, y in points:
        xx, yy = tr.transform(x, y)
        out.append((xx, yy))
    return out


def make_csv_bytes(points: List[Tuple[float, float]]) -> bytes:
    sio = StringIO()
    w = csv.writer(sio, delimiter=";")
    w.writerow(["N", "X", "Y"])
    for i, (x, y) in enumerate(points, start=1):
        w.writerow([i, x, y])
    return sio.getvalue().encode("utf-8-sig")


# ================== CADASTRE ==================
async def fetch_nspd_info(cadnum: str) -> Dict[str, Any]:
    url = "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
    params = {"thematicSearchId": "1", "query": cadnum}
    headers = {
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
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(k, str) and k.lower() in keys:
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
        lines.append(f"(Сервис вернул: {found_cad})")

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

    if len(lines) <= 1:
        raw = str(data)
        if len(raw) > 1400:
            raw = raw[:1400] + "…"
        lines.append("Не удалось уверенно выделить поля. Сырой ответ (обрезан):")
        lines.append(raw)

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
    try:
        await q.answer()
    except Exception:
        pass

    data = q.data or ""

    # global nav
    if data == "nav:root":
        reset_coords_wizard(context)
        set_mode(context, "none")
        await q.edit_message_text("Выбери раздел:", reply_markup=kb_root())
        return

    if data == "nav:mine":
        set_mode(context, "mine")
        await q.edit_message_text("Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "nav:land":
        set_mode(context, "land")
        await q.edit_message_text("Землеустройство:", reply_markup=kb_land())
        return

    # root sections
    if data == "root:mine":
        set_mode(context, "mine")
        await q.edit_message_text("Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        await q.edit_message_text("Землеустройство:", reply_markup=kb_land())
        return

    # mine menu
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        sync_globals_from_context(context)
        await q.edit_message_text(
            "📐 Пересчёт координат — настройки.\n"
            "Сначала выбери исходную/конечную СК и формат вывода.",
            reply_markup=kb_coords_main()
        )
        return

    if data == "mine:norms":
        set_mode(context, "mine_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (маркшейдерия).\n"
            "Пока заглушка (следующим шагом подключим поиск по НД).",
            reply_markup=kb_mine()
        )
        return

    if data == "mine:report":
        set_mode(context, "mine_report")
        await q.edit_message_text(
            "🧾 Составление отчёта.\n"
            "Пока заглушка (следующим шагом подключим шаблоны/генерацию).",
            reply_markup=kb_mine()
        )
        return

    # land menu
    if data == "land:cadnum":
        set_mode(context, "land_cadnum")
        context.user_data["awaiting"] = None
        await q.edit_message_text(
            "🏷️ Кадастровый номер.\n"
            "Можно ввести вручную / прислать фото / прислать файл.",
            reply_markup=kb_land_cadnum()
        )
        return

    if data == "land:norms":
        set_mode(context, "land_norms")
        await q.edit_message_text(
            "📚 Нормативная документация (землеустройство).\n"
            "Пока заглушка (позже добавим НД и поиск).",
            reply_markup=kb_land()
        )
        return

    # ====== COORDS WIZARD ======
    if data == "coords:home":
        set_mode(context, "mine_coords")
        sync_globals_from_context(context)
        await q.edit_message_text(
            "📐 Пересчёт координат — настройки.\n"
            "Выбери исходную/конечную СК и формат вывода.",
            reply_markup=kb_coords_main()
        )
        return

    if data == "coords:set_src":
        await q.edit_message_text("Выбери ИСХОДНУЮ систему координат:", reply_markup=kb_coords_pick_crs("src"))
        return

    if data == "coords:set_dst":
        await q.edit_message_text("Выбери КОНЕЧНУЮ систему координат:", reply_markup=kb_coords_pick_crs("dst"))
        return

    if data.startswith("coords:pick_src:") or data.startswith("coords:pick_dst:"):
        _, pick, rest = data.split(":", 2)  # coords:pick_src:<label>
        kind = "src" if pick == "pick_src" else "dst"
        label = rest

        preset = CRS_PRESETS.get(label)
        if not preset:
            await q.edit_message_text("Не понял выбор. Открой настройки заново.", reply_markup=kb_coords_main())
            return

        if preset["kind"] == "epsg":
            code = preset["code"]
            if kind == "src":
                context.user_data["coords_src"] = code
                context.user_data["coords_src_label"] = label
            else:
                context.user_data["coords_dst"] = code
                context.user_data["coords_dst_label"] = label
            sync_globals_from_context(context)
            await q.edit_message_text("✅ Сохранено.", reply_markup=kb_coords_main())
            return

        if preset["kind"] == "sk42_zone":
            # нужно выбрать зону
            context.user_data["coords_zone_page"] = "1"
            sync_globals_from_context(context)
            await q.edit_message_text(
                f"Выбери зону СК-42 (Гаусс-Крюгер) для {'ИСХОДНОЙ' if kind=='src' else 'КОНЕЧНОЙ'} СК:",
                reply_markup=kb_coords_pick_zone(kind)
            )
            return

    if data.startswith("coords:zone_page:"):
        page = data.split(":")[-1]
        context.user_data["coords_zone_page"] = page if page in ("1", "2") else "1"
        sync_globals_from_context(context)
        # определить, для чего открыта зона? храним временно:
        # проще: если есть флаг awaiting_zone_kind
        kind = context.user_data.get("awaiting_zone_kind", "src")
        await q.edit_message_text(
            f"Выбери зону СК-42 (Гаусс-Крюгер) для {'ИСХОДНОЙ' if kind=='src' else 'КОНЕЧНОЙ'} СК:",
            reply_markup=kb_coords_pick_zone(kind)
        )
        return

    if data.startswith("coords:zone_src:") or data.startswith("coords:zone_dst:"):
        # coords:zone_src:7
        parts = data.split(":")
        kind = "src" if parts[1] == "zone_src" else "dst"
        z = int(parts[2])

        if z < 1 or z > 60:
            await q.edit_message_text("Зона должна быть 1..60", reply_markup=kb_coords_main())
            return

        epsg = f"EPSG:{28400 + z}"
        label = f"СК-42 ГК зона {z} (EPSG:{28400+z})"

        if kind == "src":
            context.user_data["coords_src"] = epsg
            context.user_data["coords_src_label"] = label
        else:
            context.user_data["coords_dst"] = epsg
            context.user_data["coords_dst_label"] = label

        context.user_data.pop("awaiting_zone_kind", None)
        sync_globals_from_context(context)
        await q.edit_message_text("✅ Зона сохранена.", reply_markup=kb_coords_main())
        return

    if data == "coords:set_out":
        await q.edit_message_text("Выбери, как вывести результат:", reply_markup=kb_coords_pick_output())
        return

    if data.startswith("coords:out:"):
        mode = data.split(":")[-1]
        if mode not in ("chat", "csv"):
            await q.edit_message_text("Не понял формат вывода.", reply_markup=kb_coords_main())
            return
        context.user_data["coords_out_mode"] = "Показать в чате" if mode == "chat" else "Файл CSV"
        context.user_data["coords_out_mode_code"] = mode
        sync_globals_from_context(context)
        await q.edit_message_text("✅ Формат вывода сохранён.", reply_markup=kb_coords_main())
        return

    if data == "coords:ready":
        # проверка настроек
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")
        if not src or not dst or not out_mode:
            sync_globals_from_context(context)
            await q.edit_message_text(
                "Нужно выбрать ВСЁ: исходную СК, конечную СК и формат вывода.",
                reply_markup=kb_coords_main()
            )
            return
        context.user_data["awaiting"] = "coords_input"
        await q.edit_message_text(
            "✅ Готово.\n"
            "Теперь пришли координаты:\n"
            "- текстом\n"
            "- фото\n"
            "- файлом txt/csv\n\n"
            "Формат текста: каждая строка содержит 2 числа (X Y) — я возьму первые два.",
            reply_markup=kb_coords_ready()
        )
        return

    if data == "coords:manual":
        context.user_data["awaiting"] = "coords_manual"
        await q.edit_message_text(
            "✍️ Ввод координат вручную.\n"
            "Пришли:\n"
            "72853345 551668\n"
            "или список строк, в каждой строке 2 числа.",
            reply_markup=kb_coords_ready()
        )
        return

    if data == "coords:photo_help":
        context.user_data["awaiting"] = "coords_photo"
        await q.edit_message_text(
            "📷 Пришли фото с координатами.\n"
            "Я распознаю X/Y и пересчитаю по выбранным СК.\n"
            "Если где-то не уверен — поставлю '?' и попрошу перепроверить.",
            reply_markup=kb_coords_ready()
        )
        return

    if data == "coords:file_help":
        context.user_data["awaiting"] = "coords_file"
        await q.edit_message_text(
            "📎 Пришли файл .txt или .csv.\n"
            "Я возьму из каждой строки первые 2 числа как X и Y.\n"
            "Разделители могут быть пробел/таб/; /, — главное, чтобы числа были.",
            reply_markup=kb_coords_ready()
        )
        return

    # ====== CADASTRE ======
    if data == "cad:manual":
        context.user_data["awaiting"] = "cad_manual"
        await q.edit_message_text(
            "✍️ Введи кадастровый номер (формат типа 89:35:800113:31):",
            reply_markup=kb_land_cadnum()
        )
        return

    if data == "cad:photo_help":
        context.user_data["awaiting"] = "cad_photo"
        await q.edit_message_text(
            "📷 Пришли фото, где есть кадастровый номер.\n"
            "Я распознаю и попробую получить сведения.",
            reply_markup=kb_land_cadnum()
        )
        return

    if data == "cad:file_help":
        context.user_data["awaiting"] = "cad_file"
        await q.edit_message_text(
            "📎 Пришли файл .txt или .csv с кадастровыми номерами.\n"
            "Я найду все КН в тексте и выведу сведения по каждому (по очереди).",
            reply_markup=kb_land_cadnum()
        )
        return

    await q.edit_message_text("Не понял команду. Нажми /menu", reply_markup=kb_root())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    awaiting = context.user_data.get("awaiting")
    text = update.message.text or ""

    # ---- COORDS INPUT (text) ----
    if awaiting in ("coords_input", "coords_manual") or mode == "mine_coords":
        # если пользователь в координатах и прислал числа — пробуем пересчитать
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")

        if src and dst and out_mode:
            points = parse_points_from_text(text)
            if points:
                await do_transform_and_respond(update, context, points)
                return

    # ---- CAD INPUT (text) ----
    if awaiting == "cad_manual" or mode == "land_cadnum":
        cadnums = parse_cadnums_from_text(text)
        if not cadnums:
            await update.message.reply_text(
                "Не вижу корректный кадастровый номер (формат типа 89:35:800113:31). Попробуй ещё раз.",
                reply_markup=kb_land_cadnum()
            )
            return

        # если несколько — обработаем по одному
        for cad in cadnums:
            await update.message.reply_text(f"Запрашиваю сведения по КН: {cad} …")
            try:
                data_json = await fetch_nspd_info(cad)
                info = summarize_nspd_json(cad, data_json)
                await update.message.reply_text(info, reply_markup=kb_land_cadnum())
            except Exception as e:
                logger.exception("NSPD fetch failed")
                await update.message.reply_text(
                    "Не смог получить сведения (НСПД может быть недоступен/ограничивает запросы).\n"
                    f"Ошибка: {e}",
                    reply_markup=kb_land_cadnum()
                )
        return

    await update.message.reply_text("Открой /menu и выбери действие.", reply_markup=kb_root())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    awaiting = context.user_data.get("awaiting")

    photo = update.message.photo[-1]
    f = await photo.get_file()
    b = await f.download_as_bytearray()
    image_b64 = base64.b64encode(bytes(b)).decode("utf-8")

    # ---- COORDS PHOTO ----
    if awaiting == "coords_photo" or (mode == "mine_coords" and context.user_data.get("coords_src")):
        await update.message.reply_text("Распознаю координаты с фото…")

        system_add = (
            "Распознай координаты X и Y.\n"
            "Верни строго:\n"
            "TRANSCRIPTION:\n"
            "<как написано>\n"
            "PARSED:\n"
            "X=<значение или ?>\n"
            "Y=<значение или ?>\n"
        )

        try:
            raw = ask_claude_with_image("Распознай X и Y.", image_b64, system_add)
        except Exception as e:
            logger.exception("Claude photo error")
            await update.message.reply_text(f"Ошибка распознавания фото: {e}", reply_markup=kb_coords_ready())
            return

        mx = re.search(r"\bX\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        my = re.search(r"\bY\s*=\s*([0-9?,.\-+]+)", raw, re.IGNORECASE)
        x_s = (mx.group(1).strip() if mx else "")
        y_s = (my.group(1).strip() if my else "")

        if not x_s or not y_s or "?" in x_s or "?" in y_s:
            await update.message.reply_text(
                "Не уверен в распознавании.\n\n"
                f"{raw}\n\n"
                "Скопируй и пришли координаты вручную (правильно), либо пришли более чёткое фото.",
                reply_markup=kb_coords_ready()
            )
            return

        x = _clean_num(x_s)
        y = _clean_num(y_s)
        if x is None or y is None:
            await update.message.reply_text(
                "Не смог преобразовать распознанные числа.\n\n"
                f"{raw}\n\n"
                "Пришли координаты вручную.",
                reply_markup=kb_coords_ready()
            )
            return

        await do_transform_and_respond(update, context, [(x, y)])
        return

    # ---- CAD PHOTO ----
    if awaiting == "cad_photo" or mode == "land_cadnum":
        await update.message.reply_text("Распознаю кадастровый номер с фото…")

        system_add = (
            "Распознай кадастровый номер РФ.\n"
            "Не выдумывай.\n"
            "Верни:\n"
            "TRANSCRIPTION:\n"
            "<как написано>\n"
            "PARSED:\n"
            "CADNUM=<как распознал или ?>\n"
        )
        try:
            raw = ask_claude_with_image("Распознай кадастровый номер.", image_b64, system_add)
        except Exception as e:
            logger.exception("Claude photo error")
            await update.message.reply_text(f"Ошибка распознавания фото: {e}", reply_markup=kb_land_cadnum())
            return

        mc = re.search(r"\bCADNUM\s*=\s*([0-9?:]+)", raw, re.IGNORECASE)
        cad_guess = (mc.group(1).strip() if mc else "")
        if not cad_guess or "?" in cad_guess:
            await update.message.reply_text(
                "Не уверен в распознавании КН.\n\n"
                f"{raw}\n\n"
                "Пришли КН вручную (точно) или более чёткое фото.",
                reply_markup=kb_land_cadnum()
            )
            return

        cadnums = parse_cadnums_from_text(cad_guess)
        if len(cadnums) != 1:
            await update.message.reply_text(
                "Не могу уверенно выделить корректный КН.\n\n"
                f"{raw}\n\n"
                "Пришли КН вручную.",
                reply_markup=kb_land_cadnum()
            )
            return

        cad = cadnums[0]
        await update.message.reply_text(f"Распознал как: {cad}. Запрашиваю сведения…")
        try:
            data_json = await fetch_nspd_info(cad)
            info = summarize_nspd_json(cad, data_json)
            await update.message.reply_text(info, reply_markup=kb_land_cadnum())
        except Exception as e:
            logger.exception("NSPD fetch failed")
            await update.message.reply_text(
                "Не смог получить сведения (НСПД может быть недоступен/ограничивает запросы).\n"
                f"Ошибка: {e}",
                reply_markup=kb_land_cadnum()
            )
        return

    await update.message.reply_text("Открой /menu и выбери действие.", reply_markup=kb_root())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    awaiting = context.user_data.get("awaiting")

    doc = update.message.document
    if not doc:
        return

    filename = (doc.file_name or "").lower()
    if not (filename.endswith(".txt") or filename.endswith(".csv")):
        await update.message.reply_text(
            "Поддерживаю пока только .txt и .csv.\n"
            "Сохрани как txt/csv и пришли снова.",
        )
        return

    file = await doc.get_file()
    b = await file.download_as_bytearray()
    text = None
    try:
        text = bytes(b).decode("utf-8")
    except Exception:
        try:
            text = bytes(b).decode("cp1251", errors="ignore")
        except Exception:
            text = bytes(b).decode("utf-8", errors="ignore")

    # ---- COORDS FILE ----
    if awaiting == "coords_file" or (mode == "mine_coords" and context.user_data.get("coords_src")):
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")

        if not (src and dst and out_mode):
            await update.message.reply_text(
                "Сначала настрой исходную/конечную СК и формат вывода.\n"
                "Открой: Маркшейдерия → Пересчёт координат.",
                reply_markup=kb_mine()
            )
            return

        points = parse_points_from_text(text)
        if not points:
            await update.message.reply_text(
                "В файле не нашёл координаты. Нужно, чтобы в строках были числа (X Y).",
                reply_markup=kb_coords_ready()
            )
            return

        await do_transform_and_respond(update, context, points, filename_hint=os.path.splitext(filename)[0])
        return

    # ---- CAD FILE ----
    if awaiting == "cad_file" or mode == "land_cadnum":
        cadnums = parse_cadnums_from_text(text)
        if not cadnums:
            await update.message.reply_text(
                "В файле не нашёл кадастровых номеров (формат 89:35:800113:31).",
                reply_markup=kb_land_cadnum()
            )
            return

        await update.message.reply_text(f"Нашёл КН: {len(cadnums)} шт. Начинаю запрос…")
        for cad in cadnums:
            await update.message.reply_text(f"КН: {cad} …")
            try:
                data_json = await fetch_nspd_info(cad)
                info = summarize_nspd_json(cad, data_json)
                await update.message.reply_text(info)
            except Exception as e:
                logger.exception("NSPD fetch failed")
                await update.message.reply_text(f"Ошибка запроса по {cad}: {e}")
        await update.message.reply_text("Готово.", reply_markup=kb_land_cadnum())
        return

    await update.message.reply_text("Открой /menu и выбери действие.", reply_markup=kb_root())


async def do_transform_and_respond(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    points: List[Tuple[float, float]],
    filename_hint: str = "coords",
) -> None:
    src = context.user_data.get("coords_src")
    dst = context.user_data.get("coords_dst")
    out_mode = context.user_data.get("coords_out_mode_code")

    if not (src and dst and out_mode):
        await update.message.reply_text(
            "Не заданы СК/вывод. Открой: Маркшейдерия → Пересчёт координат.",
            reply_markup=kb_mine()
        )
        return

    try:
        out_points = transform_points(points, src, dst)
    except Exception as e:
        logger.exception("Transform error")
        await update.message.reply_text(
            "Не смог пересчитать. Частая причина — неправильно выбрана зона СК-42.\n"
            f"Ошибка: {e}",
            reply_markup=kb_coords_ready()
        )
        return

    if out_mode == "chat":
        await update.message.reply_text(
            "✅ Результат:\n\n" + format_points_table(out_points),
            reply_markup=kb_coords_ready()
        )
        return

    # csv
    csv_bytes = make_csv_bytes(out_points)
    bio = BytesIO(csv_bytes)
    bio.name = f"{filename_hint}_converted.csv"
    bio.seek(0)

    await update.message.reply_document(
        document=InputFile(bio),
        filename=bio.name,
        caption="✅ Готово. Результат в CSV (разделитель ';').",
        reply_markup=kb_coords_ready()
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
