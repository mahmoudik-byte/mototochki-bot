"""
Мототочки — бот-сборщик. v5 (Supabase)

Пишет не в локальный файл, а в облако Supabase — поэтому данные видны и боту,
и карте, и телефону любого. Логика та же, что была; поменялось только «куда».

Запуск:  python bot.py        (Windows)  /  python3 bot.py  (Mac)
Экспорт: python bot.py --export     (тянет из облака, кладёт points.json для карты)

ПЕРЕД ПЕРВЫМ ЗАПУСКОМ создай рядом файл secrets.txt из двух строк:

    SUPABASE_URL=https://kvtsexjoafjukomktnty.supabase.co
    SUPABASE_KEY=твой_service_role_ключ

service_role — это СЕКРЕТ. В git его не клади, никому не показывай.
Токен бота по-прежнему в token.txt (спросит сам при первом запуске).
"""

import json
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "token.txt"
SECRETS_FILE = HERE / "secrets.txt"
EXPORT_FILE = HERE / "points.json"

PY = "python" if os.name == "nt" else "python3"
NEAR_M = 150
GROW_AT = 5
NUDGE_BEFORE = 600
GEO_WAIT = 45

if sys.version_info < (3, 10):
    sys.exit(f"Нужен Python 3.10+, у тебя {sys.version.split()[0]}. Обнови с python.org")


def ensure_deps():
    missing = []
    try:
        import aiogram  # noqa: F401
    except ImportError:
        missing.append("aiogram")
    try:
        import httpx  # noqa: F401
    except ImportError:
        missing.append("httpx")
    if missing:
        print(f"Ставлю {', '.join(missing)} — это один раз.\n")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet", *missing])
        except subprocess.CalledProcessError:
            sys.exit(f"\nНе поставилось. Попробуй руками:\n"
                     f"{sys.executable} -m pip install {' '.join(missing)}")
        print("Готово.\n")


def read_secrets() -> tuple[str, str]:
    if not SECRETS_FILE.exists():
        sys.exit(
            f"\nНет файла {SECRETS_FILE.name}. Создай его рядом с ботом, две строки:\n\n"
            "SUPABASE_URL=https://kvtsexjoafjukomktnty.supabase.co\n"
            "SUPABASE_KEY=твой_service_role_ключ\n\n"
            "service_role берётся в Supabase → Get connected → API Keys → secret.")
    url = key = None
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("SUPABASE_URL="):
            url = line.split("=", 1)[1].strip().rstrip("/")
        elif line.startswith("SUPABASE_KEY="):
            key = line.split("=", 1)[1].strip()
    if not url or not key:
        sys.exit(f"В {SECRETS_FILE.name} нужны обе строки: SUPABASE_URL и SUPABASE_KEY.")
    return url, key


def get_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t:
            return t
    print("Токен от @BotFather. Вставь и нажми Enter:")
    t = input("> ").strip()
    if ":" not in t or not t.split(":")[0].isdigit():
        sys.exit("Это не похоже на токен. Он вида 8134567890:AAH... Запусти заново.")
    TOKEN_FILE.write_text(t)
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"Запомнил в {TOKEN_FILE.name}. В git его не клади.\n")
    return t


# ── Supabase через PostgREST ──────────────────────────────────────────

ensure_deps()
import httpx  # noqa: E402

SB_URL, SB_KEY = read_secrets()
REST = f"{SB_URL}/rest/v1"
HEAD = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}


def sb_upsert(table: str, row: dict, on_conflict: str | None = None):
    params = {}
    h = dict(HEAD)
    h["Prefer"] = "resolution=merge-duplicates"
    if on_conflict:
        params["on_conflict"] = on_conflict
    r = httpx.post(f"{REST}/{table}", headers=h, params=params,
                   json=row, timeout=15)
    r.raise_for_status()


def sb_insert(table: str, row: dict):
    r = httpx.post(f"{REST}/{table}", headers=HEAD, json=row, timeout=15)
    r.raise_for_status()


def sb_update(table: str, match: dict, changes: dict):
    params = {k: f"eq.{v}" for k, v in match.items()}
    r = httpx.patch(f"{REST}/{table}", headers=HEAD, params=params,
                    json=changes, timeout=15)
    r.raise_for_status()


def sb_storage_upload(path: str, data: bytes, content_type="image/jpeg") -> str:
    """Кладёт байты в публичное ведро photos, возвращает публичную ссылку."""
    url = f"{SB_URL}/storage/v1/object/photos/{path}"
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
         "Content-Type": content_type, "x-upsert": "true"}
    r = httpx.post(url, headers=h, content=data, timeout=30)
    r.raise_for_status()
    return f"{SB_URL}/storage/v1/object/public/photos/{path}"


def sb_select(table: str, select="*", **filters) -> list[dict]:
    params = {"select": select}
    for k, v in filters.items():
        params[k] = v
    r = httpx.get(f"{REST}/{table}", headers=HEAD, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_count(table: str, **filters) -> int:
    h = dict(HEAD)
    h["Prefer"] = "count=exact"
    params = {"select": "id", **filters}
    r = httpx.get(f"{REST}/{table}", headers=h, params=params, timeout=15)
    r.raise_for_status()
    rng = r.headers.get("content-range", "*/0")
    return int(rng.split("/")[-1]) if "/" in rng else 0


# ── операции ──────────────────────────────────────────────────────────

def meters(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110540
    return math.hypot(dx, dy)


def nearby_point(lat, lon, radius=NEAR_M):
    best, best_d = None, radius
    for p in sb_select("points", status="eq.live"):
        d = meters(lat, lon, p["lat"], p["lon"])
        if d < best_d:
            best, best_d = p, d
    return best


def save_point(d: dict):
    sb_upsert("points", {
        "id": d["id"], "lat": d["lat"], "lon": d["lon"], "type": d.get("type"),
        "title": d.get("title"), "note": d.get("note"), "hours": d.get("hours"),
        "status": d.get("status", "live"), "author_id": d["author_id"],
        "created_at": d["created_at"],
    }, on_conflict="id")
    for f in d.get("flags", ()):
        sb_upsert("point_flags",
                  {"point_id": d["id"], "flag": f, "votes_yes": 1, "votes_no": 0},
                  on_conflict="point_id,flag")
    if d.get("stars"):
        sb_upsert("ratings",
                  {"point_id": d["id"], "user_id": d["author_id"],
                   "stars": d["stars"], "created_at": int(time.time())},
                  on_conflict="point_id,user_id")


def save_photo(point_id, file_id, author_id, url=None) -> int:
    sb_insert("photos", {"point_id": point_id, "file_id": file_id, "url": url,
                         "author_id": author_id, "taken_at": int(time.time())})
    return sb_count("photos", point_id=f"eq.{point_id}")


def save_presence(p: dict):
    sb_upsert("presence", {
        "id": p["id"], "point_id": p.get("point_id"), "lat": p["lat"],
        "lon": p["lon"], "user_id": p["user_id"], "role": "photographer",
        "nick": p["nick"], "contact": p.get("contact"),
        "started_at": p["started_at"], "until": p["until"],
        "nudged": 0, "status": "live",
    }, on_conflict="id")


def end_presence(pid: str, status="left"):
    rows = sb_select("presence", select="lat,lon", id=f"eq.{pid}")
    sb_update("presence", {"id": pid}, {"status": status})
    if rows:
        grow_spot(rows[0]["lat"], rows[0]["lon"])


def live_presence(user_id: int):
    now = int(time.time())
    rows = sb_select("presence", user_id=f"eq.{user_id}", status="eq.live",
                     until=f"gt.{now}", order="started_at.desc", limit="1")
    return rows[0] if rows else None


def extend_presence(pid: str, hours=1) -> int:
    until = int(time.time()) + hours * 3600
    sb_update("presence", {"id": pid}, {"until": until, "nudged": 0})
    return until


def grow_spot(lat, lon):
    rows = sb_select("presence", select="lat,lon")
    n = sum(1 for r in rows if meters(lat, lon, r["lat"], r["lon"]) < NEAR_M)
    if n < GROW_AT:
        return
    for p in sb_select("points", select="lat,lon", type="eq.photo"):
        if meters(lat, lon, p["lat"], p["lon"]) < NEAR_M:
            return
    sb_insert("points", {
        "id": str(uuid.uuid4()), "lat": lat, "lon": lon, "type": "photo",
        "title": "Тут часто снимают",
        "note": f"Фотографы отмечались здесь {n} раз",
        "hours": "always", "status": "live", "author_id": 0,
        "created_at": int(time.time()),
    })
    print(f"Проросла точка «тут часто снимают»: {lat:.4f}, {lon:.4f} ({n})")


def export():
    now = int(time.time())
    out = []
    for p in sb_select("points", status="eq.live"):
        flags = {r["flag"]: [r["votes_yes"], r["votes_no"]]
                 for r in sb_select("point_flags", point_id=f"eq.{p['id']}")}
        stars = [r["stars"] for r in
                 sb_select("ratings", select="stars", point_id=f"eq.{p['id']}")]
        shots = [r["url"] for r in
                 sb_select("photos", select="url",
                           point_id=f"eq.{p['id']}", status="eq.live")
                 if r.get("url")]
        out.append({
            "id": p["id"], "lat": p["lat"], "lon": p["lon"], "type": p["type"],
            "title": p["title"] or "Без названия", "note": p["note"] or "",
            "hours": p["hours"], "status": p["status"], "flags": flags,
            "ratings": stars, "shots": shots, "checkins": [],
        })
    shooters = [{
        "id": r["id"], "lat": r["lat"], "lon": r["lon"],
        "point_id": r["point_id"], "nick": r["nick"],
        "contact": r["contact"], "until": r["until"],
    } for r in sb_select("presence", status="eq.live", until=f"gt.{now}")]
    EXPORT_FILE.write_text(json.dumps(
        {"points": out, "shooters": shooters}, ensure_ascii=False, indent=2))
    print(f"{len(out)} точек, {len(shooters)} фотографов → {EXPORT_FILE.name}")


if "--export" in sys.argv:
    export()
    sys.exit()


# ── бот ───────────────────────────────────────────────────────────────

import asyncio  # noqa: E402
from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.filters import Command, CommandStart  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

TYPES = {"food": "Поесть", "stand": "Постоять", "party": "Туса",
         "view": "Вид", "service": "Сервис"}
HOURS = {"day": "Днём", "night": "Ночью", "always": "Круглосуточно"}
FLAGS = {"no_kick": "Не гоняют", "bikes_visible": "Байки видно",
         "gear_ok": "Пустили в экипе", "group_fits": "Группа влезет"}
SPANS = {1: "1 час", 2: "2 часа", 3: "3 часа", 4: "4 часа"}

BTN_MARK = "📍 Отметить место"
BTN_HERE = "📍 Я тут сейчас"
BTN_HERE_SHOOT = "📷 Снимаю тут сейчас"
BTN_PICK = "🗺 Выбрать на карте"
BTN_PHOTOG = "📷 Я фотограф"
BTN_MAP = "🗺 На карту"
BTN_MORE = "⏱ Стою ещё час"
BTN_LEAVE = "🏁 Уехал"
BTN_BACK = "← Назад"
BTN_STATS = "📊 Сколько собрано"

MAP_URL = "https://mototochki.netlify.app"

GEO_HELP = (
    "Всё ещё жду точку. Если жал «Я тут сейчас», а ничего не произошло — "
    "скорее всего айфон режет геолокацию.\n\n"
    "<b>Разрешить:</b> Настройки → Конфиденциальность и безопасность → "
    "Службы геолокации → Telegram → «При использовании». "
    "Потом закрой телеграм совсем и открой заново.\n\n"
    "<b>Либо просто выбери на карте</b> — кнопка внизу, разрешение не нужно."
)
PICK_HELP = (
    "Выбрать точку на карте:\n"
    "скрепка 📎 → <b>Геопозиция</b> → тащи карту пальцем, пин в центре → "
    "«Отправить выбранную геопозицию».\n\n"
    "Можно вбить адрес поиском. Геолокация для этого не нужна."
)

drafts: dict[int, dict] = {}
modes: dict[int, str] = {}
geo_waiters: dict[int, float] = {}
dp = Dispatcher()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def menu(user_id: int) -> ReplyKeyboardMarkup:
    if modes.get(user_id) == "shoot":
        rows = [[KeyboardButton(text=BTN_HERE_SHOOT, request_location=True)],
                [KeyboardButton(text=BTN_PICK)], [KeyboardButton(text=BTN_BACK)]]
    elif modes.get(user_id) == "mark":
        rows = [[KeyboardButton(text=BTN_HERE, request_location=True)],
                [KeyboardButton(text=BTN_PICK)], [KeyboardButton(text=BTN_BACK)]]
    elif live_presence(user_id):
        rows = [[KeyboardButton(text=BTN_MARK)],
                [KeyboardButton(text=BTN_MORE), KeyboardButton(text=BTN_LEAVE)],
                [KeyboardButton(text=BTN_MAP)]]
    else:
        rows = [[KeyboardButton(text=BTN_MARK)],
                [KeyboardButton(text=BTN_PHOTOG), KeyboardButton(text=BTN_STATS)],
                [KeyboardButton(text=BTN_MAP)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               is_persistent=True)


def kb_flags(d):
    rows = [[btn(("✅ " if k in d["flags"] else "") + v, f"flag:{k}")]
            for k, v in FLAGS.items()]
    rows.append([btn("Дальше →", "step:stars")])
    return kb(rows)


def head(d) -> str:
    bits = []
    if d.get("type"):
        bits.append(TYPES[d["type"]])
    if d.get("hours"):
        bits.append(HOURS[d["hours"]])
    return " · ".join(bits) or f'{d["lat"]:.4f}, {d["lon"]:.4f}'


def hhmm(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


async def need(c: CallbackQuery):
    d = drafts.get(c.from_user.id)
    if not d:
        await c.answer("Черновик потерялся — бот перезапускался. Начни заново.",
                       show_alert=True)
    return d


async def home(m: Message, text: str):
    await m.answer(text, parse_mode="HTML", reply_markup=menu(m.chat.id))


def arm_geo(user_id: int):
    geo_waiters[user_id] = time.time() + GEO_WAIT


def disarm_geo(user_id: int):
    geo_waiters.pop(user_id, None)


@dp.message(CommandStart())
async def start(m: Message):
    modes.pop(m.chat.id, None)
    disarm_geo(m.chat.id)
    p = live_presence(m.chat.id)
    if p:
        return await home(m, f"📷 Ты на карте как <b>{p['nick']}</b> "
                             f"до {hhmm(p['until'])}.\n\nЖми что нужно ↓")
    await home(
        m,
        "<b>Карта мест, где мотоциклисту рады.</b>\n"
        "Где поесть, постоять, потусить, где не гоняют и где снимают фото.\n\n"
        "📍 <b>Отметить место</b> — добавить точку на карту.\n"
        "📷 <b>Я фотограф</b> — попасть на карту с ником, чтобы тебя нашли.\n"
        "🗺 <b>На карту</b> — посмотреть все точки на карте.\n"
        "📊 <b>Сколько собрано</b> — сколько точек уже есть.\n\n"
        "Жми кнопку внизу ↓")


@dp.message(F.text == BTN_MAP)
@dp.message(Command("map"))
async def open_map(m: Message):
    await m.answer(
        "Вот карта всех точек — жми, откроется в браузере 👇",
        reply_markup=kb([[InlineKeyboardButton(text="🗺 Открыть карту", url=MAP_URL)]]))


@dp.message(F.text == BTN_STATS)
@dp.message(Command("stats"))
async def stats(m: Message):
    n = sb_count("points", status="eq.live")
    ph = sb_count("photos")
    sp = sb_count("points", type="eq.photo")
    live = sb_count("presence", status="eq.live", until=f"gt.{int(time.time())}")
    await home(m, f"Точек: <b>{n}</b> (проросших: {sp})\nФото: {ph}\n"
                  f"Фотографов сейчас: {live}")


# ── режимы ────────────────────────────────────────────────────────────

@dp.message(F.text == BTN_MARK)
async def mark_menu(m: Message):
    modes[m.chat.id] = "mark"
    disarm_geo(m.chat.id)
    await home(m, "Отмечаешь место. Как задать точку? ↓\n\n"
                  "📍 <b>Я тут сейчас</b> — координаты с телефона.\n"
                  "🗺 <b>Выбрать на карте</b> — если стоишь не там или вспомнил дома.\n\n"
                  "Передумал — жми <b>← Назад</b>.")


@dp.message(F.text == BTN_PHOTOG)
async def photog_menu(m: Message):
    if live_presence(m.chat.id):
        return await start(m)
    modes[m.chat.id] = "shoot"
    disarm_geo(m.chat.id)
    await home(m, "На карту как фотограф. Где ты? ↓\n\n"
                  "📷 <b>Снимаю тут сейчас</b> — координаты с телефона.\n"
                  "🗺 <b>Выбрать на карте</b> — если отмечаешь заранее.\n\n"
                  "Передумал — жми <b>← Назад</b>.")


@dp.message(F.text == BTN_PICK)
async def pick_on_map(m: Message):
    if modes.get(m.chat.id) not in ("mark", "shoot"):
        return await home(m, "Сначала выбери, что отмечаешь ↓")
    disarm_geo(m.chat.id)
    await home(m, PICK_HELP)


@dp.message(F.text == BTN_BACK)
async def back(m: Message):
    modes.pop(m.chat.id, None)
    drafts.pop(m.chat.id, None)
    disarm_geo(m.chat.id)
    await home(m, "Ок.")


@dp.message(F.text == BTN_LEAVE)
async def leave_btn(m: Message):
    p = live_presence(m.chat.id)
    if not p:
        return await home(m, "Ты и так не на карте.")
    end_presence(p["id"])
    await home(m, "Снял тебя с карты. Хорошей дороги.")


@dp.message(F.text == BTN_MORE)
async def more_btn(m: Message):
    p = live_presence(m.chat.id)
    if not p:
        return await home(m, "Тебя нет на карте. Жми «Я фотограф».")
    until = extend_presence(p["id"])
    await home(m, f"Продлил до <b>{hhmm(until)}</b>.")


async def geo_watchdog(bot):
    while True:
        now = time.time()
        for uid, deadline in list(geo_waiters.items()):
            if now >= deadline:
                geo_waiters.pop(uid, None)
                if modes.get(uid) in ("mark", "shoot"):
                    try:
                        await bot.send_message(uid, GEO_HELP, parse_mode="HTML",
                                               reply_markup=menu(uid))
                    except Exception:
                        pass
        await asyncio.sleep(3)


# ── фотограф ──────────────────────────────────────────────────────────

async def shoot_place(m: Message, d: dict):
    near = nearby_point(d["lat"], d["lon"])
    if near and near["type"] != "photo":
        d["near"] = near
        return await m.answer(
            f"Ты у «{near['title']}»?",
            reply_markup=kb([[btn("Да, я там", "sp:yes")],
                             [btn("Нет, другое место", "sp:no")]]))
    d["point_id"] = near["id"] if near else None
    await ask_nick(m, d)


async def ask_nick(m: Message, d: dict):
    u = m.chat.username or m.chat.first_name or "Фотограф"
    d["nick"] = u
    await m.answer(f"Показывать тебя на карте как <b>{u}</b>?", parse_mode="HTML",
                   reply_markup=kb([[btn("Да", "nick:ok")],
                                    [btn("Другой ник", "nick:edit")]]))


@dp.callback_query(F.data.startswith("sp:"))
async def shoot_place_pick(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["point_id"] = d["near"]["id"] if c.data == "sp:yes" else None
    await c.message.edit_reply_markup(reply_markup=None)
    await ask_nick(c.message, d)
    await c.answer()


@dp.callback_query(F.data == "nick:edit")
async def nick_edit(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["awaiting"] = "nick"
    await c.message.edit_text("Как тебя подписать? Одним сообщением.")
    await c.answer()


@dp.callback_query(F.data == "nick:ok")
async def nick_ok(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    await c.message.edit_reply_markup(reply_markup=None)
    await ask_span(c.message, d)
    await c.answer()


async def ask_span(m: Message, d: dict):
    d["awaiting"] = None
    await m.answer(
        f"<b>{d['nick']}</b> — до скольки стоишь?\n"
        "Ставь меньше, чем думаешь: продлить одна кнопка, "
        "а протухший пин убивает доверие ко всей карте.",
        parse_mode="HTML",
        reply_markup=kb([[btn(v, f"span:{k}")] for k, v in SPANS.items()]))


@dp.callback_query(F.data.startswith("span:"))
async def pick_span(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    hours = int(c.data.split(":")[1])
    now = int(time.time())
    p = {"id": str(uuid.uuid4()), "point_id": d.get("point_id"),
         "lat": d["lat"], "lon": d["lon"], "user_id": c.from_user.id,
         "nick": d["nick"], "contact": c.from_user.username,
         "started_at": now, "until": now + hours * 3600}
    save_presence(p)
    drafts.pop(c.from_user.id, None)
    modes.pop(c.from_user.id, None)
    await c.message.edit_text(f"📷 <b>{p['nick']}</b> на карте до {hhmm(p['until'])}.",
                              parse_mode="HTML")
    await home(c.message, "Пну за 10 минут до конца.\n"
                          "Уедешь раньше — жми «Уехал» внизу.")
    await c.answer()


@dp.callback_query(F.data.startswith("go:"))
async def go_home(c: CallbackQuery):
    end_presence(c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=None)
    await home(c.message, "Снял тебя с карты. Хорошей дороги.")
    await c.answer()


@dp.callback_query(F.data.startswith("more:"))
async def stay_more(c: CallbackQuery):
    until = extend_presence(c.data.split(":")[1])
    await c.message.edit_text(f"Продлил до {hhmm(until)}.")
    await c.answer()


async def expiry_loop(bot):
    while True:
        try:
            now = int(time.time())
            soon = sb_select("presence", status="eq.live", nudged="eq.0",
                             until=f"lt.{now + NUDGE_BEFORE}")
            for r in soon:
                sb_update("presence", {"id": r["id"]}, {"nudged": 1})
                try:
                    await bot.send_message(
                        r["user_id"],
                        f"Твоё время на карте кончается в {hhmm(r['until'])}. "
                        f"Ты ещё там?",
                        reply_markup=kb([[btn("Стою ещё час", f"more:{r['id']}")],
                                         [btn("Уехал", f"go:{r['id']}")]]))
                except Exception:
                    pass
            dead = sb_select("presence", select="id,user_id",
                             status="eq.live", until=f"lt.{now}")
            for r in dead:
                end_presence(r["id"], "expired")
                try:
                    await bot.send_message(
                        r["user_id"],
                        "Время вышло — снял тебя с карты. Лучше так, чем "
                        "кто-то приедет на пустое место.",
                        reply_markup=menu(r["user_id"]))
                except Exception:
                    pass
        except Exception as e:
            print(f"expiry_loop: {e}")
        await asyncio.sleep(60)


# ── место ─────────────────────────────────────────────────────────────

@dp.message(F.location | F.venue)
async def got_location(m: Message):
    disarm_geo(m.chat.id)
    loc = m.venue.location if m.venue else m.location
    lat, lon = loc.latitude, loc.longitude
    # если выбрали именованное место — подставим его название в черновик
    venue_title = m.venue.title if m.venue else None

    if modes.get(m.chat.id) == "shoot":
        d = drafts.setdefault(m.chat.id, {})
        d.update({"lat": lat, "lon": lon})
        return await shoot_place(m, d)

    near = nearby_point(lat, lon)
    if near:
        drafts[m.chat.id] = {"lat": lat, "lon": lon, "near": near}
        return await m.answer(
            f"Рядом уже есть «{near['title']}». Это оно?",
            reply_markup=kb([[btn("Да, это оно", "dupe:yes")],
                             [btn("Нет, новое место", "dupe:no")]]))
    await new_point(m, lat, lon, venue_title)


async def new_point(m: Message, lat, lon, venue_title=None):
    modes.pop(m.chat.id, None)
    drafts[m.chat.id] = {
        "id": str(uuid.uuid4()), "lat": lat, "lon": lon,
        "author_id": m.chat.id, "flags": set(), "stars": None,
        "title": venue_title, "note": None, "awaiting": None,
        "created_at": int(time.time()),
    }
    hint = f"\n\nНазвание с карты: <b>{venue_title}</b> (можно поменять потом)" if venue_title else ""
    await m.answer(f"📍 {lat:.4f}, {lon:.4f}{hint}\n\nЧто это за место?",
                   parse_mode="HTML",
                   reply_markup=kb([[btn(v, f"type:{k}")] for k, v in TYPES.items()]))


@dp.callback_query(F.data.startswith("dupe:"))
async def dupe(c: CallbackQuery):
    d = drafts.get(c.from_user.id)
    if not d:
        return await c.answer("Черновик потерялся.", show_alert=True)
    await c.message.edit_reply_markup(reply_markup=None)
    if c.data == "dupe:yes":
        drafts.pop(c.from_user.id, None)
        await home(c.message, f"Ок, дубль не плодим. "
                              f"«{d['near']['title']}» уже на карте.")
    else:
        await new_point(c.message, d["lat"], d["lon"])
    await c.answer()


@dp.callback_query(F.data.startswith("type:"))
async def pick_type(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["type"] = c.data.split(":")[1]
    await c.message.edit_text(f"{head(d)}\n\nКогда это место живое?",
                              reply_markup=kb([[btn(v, f"hours:{k}")]
                                               for k, v in HOURS.items()]))
    await c.answer()


@dp.callback_query(F.data.startswith("hours:"))
async def pick_hours(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["hours"] = c.data.split(":")[1]
    await c.message.edit_text(f"{head(d)}\n\nКак оно тут? Жми что верно.",
                              reply_markup=kb_flags(d))
    await c.answer()


@dp.callback_query(F.data.startswith("flag:"))
async def toggle_flag(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["flags"] ^= {c.data.split(":")[1]}
    await c.message.edit_reply_markup(reply_markup=kb_flags(d))
    await c.answer()


@dp.callback_query(F.data == "step:stars")
async def ask_stars(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    row = [btn("★" * i, f"stars:{i}") for i in range(1, 6)]
    await c.message.edit_text(f"{head(d)}\n\nСколько звёзд?",
                              reply_markup=kb([row[:3], row[3:]]))
    await c.answer()


@dp.callback_query(F.data.startswith("stars:"))
async def pick_stars(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    d["stars"] = int(c.data.split(":")[1])
    d["awaiting"] = "title"
    await c.message.edit_text(
        f"{head(d)} · {'★' * d['stars']}\n\nНазвание — одним сообщением.",
        reply_markup=kb([[btn("Без названия", "step:save")]]))
    await c.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def got_text(m: Message):
    d = drafts.get(m.chat.id)
    if not d or not d.get("awaiting"):
        if modes.get(m.chat.id) in ("mark", "shoot"):
            return await home(m, "Мне нужна геолокация, а не текст.\n" + GEO_HELP)
        return await home(m, "Жми кнопки внизу ↓")

    if d["awaiting"] == "nick":
        d["nick"] = m.text.strip()[:24]
        return await ask_span(m, d)

    if d["awaiting"] == "title":
        d["title"] = m.text.strip()[:60]
        d["awaiting"] = "note"
        return await m.answer("Заметка — что важно знать. Коротко.",
                              reply_markup=kb([[btn("Готово", "step:save")]]))

    if d["awaiting"] == "note":
        d["note"] = m.text.strip()[:200]
        await finish(m, d)


@dp.callback_query(F.data == "step:save")
async def save_btn(c: CallbackQuery):
    d = await need(c)
    if not d:
        return
    await c.message.edit_reply_markup(reply_markup=None)
    await finish(c.message, d)
    await c.answer()


async def finish(m: Message, d: dict):
    d["awaiting"] = "photo"
    try:
        save_point(d)
    except Exception as e:
        return await home(m, f"⚠️ Не смог записать в базу: {e}\n"
                             "Проверь secrets.txt и интернет. Точка не сохранена.")
    flags = " · ".join(FLAGS[k] for k in d["flags"]) or "без пометок"
    await home(
        m,
        f"✅ <b>{d['title'] or 'Без названия'}</b>\n"
        f"{head(d)} · {'★' * (d['stars'] or 0)}\n{flags}\n\n"
        "Кинь фото — прикрепится к этой точке.\n"
        "Или жми «Отметить место» для следующей ↓")


@dp.message(F.photo)
async def got_photo(m: Message):
    d = drafts.get(m.chat.id)
    if not d or not d.get("id"):
        return await home(m, "Сначала отметь место ↓")
    file_id = m.photo[-1].file_id
    url = None
    try:
        # скачиваем картинку из телеграма и кладём в Storage
        tg_file = await m.bot.get_file(file_id)
        buf = await m.bot.download_file(tg_file.file_path)
        data = buf.read() if hasattr(buf, "read") else buf
        name = f"{d['id']}/{uuid.uuid4().hex}.jpg"
        url = sb_storage_upload(name, data)
    except Exception as e:
        print(f"photo upload failed: {e}")  # не роняем — сохраним хотя бы file_id
    try:
        n = save_photo(d["id"], file_id, m.chat.id, url)
    except Exception as e:
        return await home(m, f"⚠️ Фото не записалось: {e}")
    extra = "" if url else "\n(картинка не залилась в хранилище, но точка её помнит)"
    await home(m, f"Фото добавлено. У точки их {n}.{extra}\n"
                  "Ещё фото или следующая точка ↓")


async def main():
    bot = Bot(get_token())
    try:
        me = await bot.get_me()
    except Exception as e:
        cls = type(e).__name__
        if "Unauthorized" in cls:
            TOKEN_FILE.unlink(missing_ok=True)
            sys.exit("Токен не принят — стёр его. Запусти заново и вставь верный.")
        if "Conflict" in cls:
            sys.exit("Бот уже запущен где-то ещё. Два процесса на один токен нельзя.")
        sys.exit(f"Телеграм недоступен: {e}")

    # проверка связи с Supabase на старте — лучше упасть сразу, чем на первой точке
    try:
        sb_count("points")
    except Exception as e:
        sys.exit(f"Не достучался до Supabase: {e}\n"
                 "Проверь SUPABASE_URL и SUPABASE_KEY в secrets.txt.")

    asyncio.create_task(expiry_loop(bot))
    asyncio.create_task(geo_watchdog(bot))
    print(f"@{me.username} слушает, база в облаке. Ctrl+C — стоп.\n")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлен.")
