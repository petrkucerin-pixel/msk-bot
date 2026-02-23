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


# ================== CRS OPTIONS (SHORT ASCII IDS ONLY!) ==================
# callback_data must be <= 64 bytes; кириллица и длинные строки ломают.
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


# ================== UI GLOBALS ==================
_GLOBAL_CTX: Dict[str, Any] = {}


def _set(k: str, v: Any) -> None:
    _GLOBAL_CTX[k] = v


def _get(k: str, default: Any = None) -> Any:
    return _GLOBAL_CTX.get(k, default)


# ================== SAFE ANSWER / SAFE EDIT ==================
async def safe_answer(q) -> None:
    try:
        await q.answer()
    except Exception:
        pass


async def safe_edit(q, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning(f"edit_message_text BadRequest: {e}")
        # fallback: send new message
        try:
            await q.message.reply_text(text, reply_markup=reply_markup)
        except Exception as e2:
            logger.warning(f"fallback reply_text failed: {e2}")


# ================== UI HELPERS ==================
def _assert_cb(cb: str) -> str:
    # Telegram limit: 64 bytes
    b = cb.encode("utf-8")
    if len(b) > 64:
        logger.error(f"callback_data too long ({len(b)} bytes): {cb!r}")
        # deliberately shorten hard (will be handled as unknown command)
        return "cb:too_long"
    return cb


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


def kb_coords_main() -> InlineKeyboardMarkup:
    src = _get("coords_src_label", "не выбрана")
    dst = _get("coords_dst_label", "не выбрана")
    out = _get("coords_out_mode", "не выбран")

    rows = [
        [InlineKeyboardButton(f"1) Исходная СК: {src}", callback_data=_assert_cb("coords:set_src"))],
        [InlineKeyboardButton(f"2) Конечная СК: {dst}", callback_data=_assert_cb("coords:set_dst"))],
        [InlineKeyboardButton(f"3) Вывод: {out}", callback_data=_assert_cb("coords:set_out"))],
        [InlineKeyboardButton("✅ Готово: прислать координаты", callback_data=_assert_cb("coords:ready"))],
    ]
    rows += kb_nav(back_to="nav:mine", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_crs(kind: str) -> InlineKeyboardMarkup:
    # kind: src/dst
    rows: List[List[InlineKeyboardButton]] = []
    for crs_id, meta in CRS_OPTIONS.items():
        label = meta["label"]
        cb = _assert_cb(f"coords:pick:{kind}:{crs_id}")  # SHORT ASCII ONLY
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows += kb_nav(back_to="coords:home", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_zone(kind: str) -> InlineKeyboardMarkup:
    page = _get("coords_zone_page", "1")
    page = page if page in ("1", "2") else "1"
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

    rows += kb_nav(back_to="coords:set_src" if kind == "src" else "coords:set_dst", include_menu=True)
    return InlineKeyboardMarkup(rows)


def kb_coords_pick_output() -> InlineKeyboardMarkup:
    rows = []
    for label, mode in OUTPUT_PRESETS.items():
        rows.append([InlineKeyboardButton(label, callback_data=_assert_cb(f"coords:out:{mode}"))])
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


def kb_coords_ready() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✍️ Ввести координаты вручную", callback_data=_assert_cb("coords:manual"))],
        [InlineKeyboardButton("📷 Прислать фото координат", callback_data=_assert_cb("coords:photo_help"))],
        [InlineKeyboardButton("📎 Прислать файл (txt/csv) с координатами", callback_data=_assert_cb("coords:file_help"))],
        [InlineKeyboardButton("🔁 Сменить настройки СК/вывода", callback_data=_assert_cb("coords:home"))],
    ]
    rows += kb_nav(back_to="coords:home", include_menu=True)
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


def sync_globals_from_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    _set("coords_src_label", context.user_data.get("coords_src_label", "не выбрана"))
    _set("coords_dst_label", context.user_data.get("coords_dst_label", "не выбрана"))
    _set("coords_out_mode", context.user_data.get("coords_out_mode", "не выбран"))
    _set("coords_zone_page", context.user_data.get("coords_zone_page", "1"))


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


def format_points_table(points: List[Tuple[float, float]]) -> str:
    lines = ["N;X;Y"]
    for i, (x, y) in enumerate(points, start=1):
        lines.append(f"{i};{x};{y}")
    return "\n".join(lines)


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

    # global nav
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

    # root sections
    if data == "root:mine":
        set_mode(context, "mine")
        await safe_edit(q, "Маркшейдерия:", reply_markup=kb_mine())
        return

    if data == "root:land":
        set_mode(context, "land")
        await safe_edit(q, "Землеустройство:", reply_markup=kb_land())
        return

    # mine menu
    if data == "mine:coords":
        set_mode(context, "mine_coords")
        sync_globals_from_context(context)
        await safe_edit(
            q,
            "📐 Пересчёт координат — настройки.\n"
            "Сначала выбери исходную/конечную СК и формат вывода.",
            reply_markup=kb_coords_main()
        )
        return

    # coords wizard
    if data == "coords:home":
        set_mode(context, "mine_coords")
        sync_globals_from_context(context)
        await safe_edit(q, "📐 Пересчёт координат — настройки.", reply_markup=kb_coords_main())
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
            await safe_edit(q, "Не понял выбор.", reply_markup=kb_coords_main())
            return

        kind = parts[2]
        crs_id = parts[3]
        meta = CRS_OPTIONS.get(crs_id)
        if not meta:
            await safe_edit(q, "Не понял выбор.", reply_markup=kb_coords_main())
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
            sync_globals_from_context(context)
            await safe_edit(q, "✅ Сохранено.", reply_markup=kb_coords_main())
            return

        # sk42 zone
        if meta["kind"] == "sk42_zone":
            context.user_data["coords_zone_page"] = "1"
            context.user_data["awaiting_zone_kind"] = kind
            sync_globals_from_context(context)
            await safe_edit(q, "Выбери зону СК-42 (Гаусс-Крюгер):", reply_markup=kb_coords_pick_zone(kind))
            return

    if data.startswith("coords:zone_page:"):
        page = data.split(":")[-1]
        context.user_data["coords_zone_page"] = page if page in ("1", "2") else "1"
        sync_globals_from_context(context)
        kind = context.user_data.get("awaiting_zone_kind", "src")
        await safe_edit(q, "Выбери зону СК-42 (Гаусс-Крюгер):", reply_markup=kb_coords_pick_zone(kind))
        return

    if data.startswith("coords:zone:"):
        # coords:zone:src:42
        parts = data.split(":")
        if len(parts) != 4:
            await safe_edit(q, "Не понял выбор зоны.", reply_markup=kb_coords_main())
            return

        kind = parts[2]
        z = int(parts[3])
        if z < 1 or z > 60:
            await safe_edit(q, "Зона должна быть 1..60", reply_markup=kb_coords_main())
            return

        epsg = f"EPSG:{28400 + z}"
        label = f"СК-42 ГК зона {z} (EPSG:{28400+z})"
        if kind == "src":
            context.user_data["coords_src"] = epsg
            context.user_data["coords_src_label"] = label
        else:
            context.user_data["coords_dst"] = epsg
            context.user_data["coords_dst_label"] = label

        sync_globals_from_context(context)
        await safe_edit(q, "✅ Зона сохранена.", reply_markup=kb_coords_main())
        return

    if data == "coords:set_out":
        await safe_edit(q, "Выбери формат вывода:", reply_markup=kb_coords_pick_output())
        return

    if data.startswith("coords:out:"):
        mode = data.split(":")[-1]
        if mode not in ("chat", "csv"):
            await safe_edit(q, "Не понял формат вывода.", reply_markup=kb_coords_main())
            return
        context.user_data["coords_out_mode"] = "Показать в чате" if mode == "chat" else "Файл CSV"
        context.user_data["coords_out_mode_code"] = mode
        sync_globals_from_context(context)
        await safe_edit(q, "✅ Формат вывода сохранён.", reply_markup=kb_coords_main())
        return

    if data == "coords:ready":
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")
        if not src or not dst or not out_mode:
            await safe_edit(q, "Нужно выбрать исходную СК, конечную СК и вывод.", reply_markup=kb_coords_main())
            return
        context.user_data["awaiting"] = "coords_input"
        await safe_edit(q, "✅ Пришли координаты текстом (каждая строка: X Y).", reply_markup=kb_coords_ready())
        return

    if data == "coords:manual":
        context.user_data["awaiting"] = "coords_manual"
        await safe_edit(q, "✍️ Пришли координаты (каждая строка: X Y).", reply_markup=kb_coords_ready())
        return

    if data == "coords:file_help":
        context.user_data["awaiting"] = "coords_file"
        await safe_edit(q, "📎 Пришли файл .txt/.csv с координатами.", reply_markup=kb_coords_ready())
        return

    if data == "coords:photo_help":
        context.user_data["awaiting"] = "coords_photo"
        await safe_edit(q, "📷 Пришли фото с координатами.", reply_markup=kb_coords_ready())
        return

    await safe_edit(q, "Не понял команду. /menu", reply_markup=kb_root())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    text = update.message.text or ""

    if awaiting in ("coords_input", "coords_manual"):
        src = context.user_data.get("coords_src")
        dst = context.user_data.get("coords_dst")
        out_mode = context.user_data.get("coords_out_mode_code")
        if src and dst and out_mode:
            pts = parse_points_from_text(text)
            if not pts:
                await update.message.reply_text("Не вижу 2 числа X Y. Пример: 72853345 551668")
                return
            await do_transform_and_respond(update, context, pts)
            return

    # cad manual (минимально)
    if awaiting == "cad_manual":
        cadnums = parse_cadnums_from_text(text)
        if not cadnums:
            await update.message.reply_text("Не вижу КН. Пример: 89:35:800113:31")
            return
        cad = cadnums[0]
        await update.message.reply_text(f"Запрашиваю сведения по КН: {cad} …")
        try:
            data_json = await fetch_nspd_info(cad)
            await update.message.reply_text(str(data_json)[:1500] + ("…" if len(str(data_json)) > 1500 else ""))
        except Exception as e:
            await update.message.reply_text(f"Не смог получить сведения: {e}")
        return

    await update.message.reply_text("Открой /menu", reply_markup=kb_root())


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
        await update.message.reply_text(f"Ошибка пересчёта: {e}")
        return

    if out_mode == "chat":
        await update.message.reply_text("✅ Результат:\n\n" + format_points_table(out_points), reply_markup=kb_coords_ready())
        return

    csv_bytes = make_csv_bytes(out_points)
    bio = BytesIO(csv_bytes)
    bio.name = f"{filename_hint}_converted.csv"
    bio.seek(0)

    await update.message.reply_document(
        document=InputFile(bio),
        filename=bio.name,
        caption="✅ Готово. CSV (разделитель ';').",
        reply_markup=kb_coords_ready()
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Произошёл временный сбой. Повтори действие.")
    except Exception:
        pass


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("msk-bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
