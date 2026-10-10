"""Мафия 18+ — Telegram-бот для групп, у каждого игрока свой телефон.

Постоянный процесс на ПК: long polling, как у Печкина, — без белого IP и открытых портов.
Запуск: pythonw mafia_bot.py (задача Планировщика KrokodilMafiaBot, см. install_task.ps1).
Токен — bot/token.txt (в .gitignore). База партий и лог — bot/data/.
Партии лежат в SQLite: после перезапуска ПК бот продолжает с той же фазы.
"""
import base64
import html
import random
import io
import json
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import mafia_engine as E
import party_engine as P
import texts as T

try:
    import cards as C  # картинки (Pillow); без него бот работает текстом
except ImportError:
    C = None

APP_URL = "https://enolaaupurdyavd69-a11y.github.io/krokodil/"
MAFIA_URL = APP_URL + "mafia.html"
APP_VER = "3"  # меняется вместе с VER в app.js — сбрасывает кеш Telegram

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB_PATH = DATA / "mafia.db"
LOG_PATH = DATA / "bot.log"
TOKEN_PATH = BASE / "token.txt"

NIGHT_SEC = 60
DAY_SEC = 120
VOTE_SEC = 45
LOBBY_SEC = 30 * 60
RESUME_GRACE = 30  # после простоя бота даём столько секунд на текущую фазу
PARTY_TURN_SEC = 120  # Крокодил/Табу в группе: время на одно слово
PARTY_IDLE_SEC = 2 * 3600  # без ведущего столько — игра закрывается

API = ""
BOT = ""
GAMES = {}  # chat -> состояние партии (Мафия или, если kind == "party", Крокодил/Табу)
DECKS = None  # словари Крокодила и Табу, грузятся при первой игре
db = None
now = time.time  # подменяется в тестах


# ---------- лог ----------

def log(msg):
    try:
        DATA.mkdir(exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".old.log"))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except OSError:
        pass


# ---------- Telegram ----------

class TgError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"{code}: {desc}")
        self.code, self.desc = code, desc or ""


def api(method, **params):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params).encode("ascii"),  # \uXXXX — кодировка консоли Windows не мешает
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + 15) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except ValueError:
            raise TgError(e.code, str(e)) from None
    if not res.get("ok"):
        raise TgError(res.get("error_code"), res.get("description"))
    return res["result"]


def safe(method, **params):
    """Вызов, ошибка которого не должна ронять партию: пишем в лог и идём дальше."""
    try:
        return api(method, **params)
    except TgError as e:
        if "message is not modified" not in e.desc:
            log(f"{method}: {e}")
    except OSError as e:
        log(f"{method}: сеть: {e}")
    return None


def api_upload(method, files, **params):
    """Запрос с файлами (multipart) — для отправки картинок."""
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in params.items():
        v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    for k, (fname, data) in files.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   'Content-Type: image/png\r\n\r\n'.encode("utf-8"))
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(API + method, data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except ValueError:
            raise TgError(e.code, str(e)) from None
    if not res.get("ok"):
        raise TgError(res.get("error_code"), res.get("description"))
    return res["result"]


def kb(rows):
    return {"inline_keyboard": rows}


REMOVE_KEYBOARD = {"remove_keyboard": True}


def is_bot(uid):
    """Виртуальные игроки — id вида «b1», им ничего не отправляем."""
    return str(uid).startswith("b")


def send(chat, text, rows=None, markup=None):
    if is_bot(chat):
        return {"message_id": 0}
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows or markup:
        p["reply_markup"] = markup or kb(rows)
    return safe("sendMessage", **p)


def send_photo(chat, make, caption, rows=None, markup=None):
    """Картинка с подписью. make() рисует PNG; не вышло — отправляем ту же подпись текстом."""
    if is_bot(chat):
        return {"message_id": 0}
    data = None
    if C:
        try:
            data = make()
        except Exception:
            log("картинка: " + traceback.format_exc())
    if data:
        p = {"chat_id": chat, "parse_mode": "HTML"}
        long_caption = len(caption) > 1024  # лимит подписи к фото — отправим текст отдельно
        if not long_caption:
            p["caption"] = caption
            if rows or markup:
                p["reply_markup"] = markup or kb(rows)
        try:
            m = api_upload("sendPhoto", {"photo": ("card.png", data)}, **p)
            return send(chat, caption, rows, markup) if long_caption else m
        except (TgError, OSError) as e:
            log(f"sendPhoto: {e}")
    return send(chat, caption, rows, markup)


def edit(chat, msg_id, text, rows=None):
    if msg_id:
        safe("editMessageText", chat_id=chat, message_id=msg_id, text=text, parse_mode="HTML",
             disable_web_page_preview=True, reply_markup=kb(rows or []))


def answer(cq, text="", alert=False):
    safe("answerCallbackQuery", callback_query_id=cq["id"], text=text, show_alert=alert)


ALL_PERMS = ["can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos", "can_send_videos",
             "can_send_video_notes", "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
             "can_add_web_page_previews"]


def set_muted(g, uid, muted):
    if is_bot(uid):
        return
    perms = {k: not muted for k in ALL_PERMS}
    ok = safe("restrictChatMember", chat_id=g["chat"], user_id=int(uid), permissions=perms,
              use_independent_chat_permissions=True)
    if muted and ok:
        g.setdefault("restricted", []).append(uid)
    if muted and not ok and not g.get("mute_warned"):
        g["mute_warned"] = True
        send(g["chat"], "🤐 Мёртвые, пожалуйста, молчите. Сделайте бота админом группы с правом "
                        "«Блокировка участников» — буду следить сам.")


def is_admin(chat, uid):
    m = safe("getChatMember", chat_id=chat, user_id=int(uid))
    return bool(m) and m.get("status") in ("creator", "administrator")


# ---------- хранение ----------

def db_init(path=None):
    global db
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(str(path or DB_PATH))
    db.execute("CREATE TABLE IF NOT EXISTS games (chat TEXT PRIMARY KEY, state TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    db.commit()


def load_games():
    return {c: json.loads(s) for c, s in db.execute("SELECT chat, state FROM games")}


def save(g):
    db.execute("INSERT OR REPLACE INTO games VALUES (?, ?)", (g["chat"], json.dumps(g, ensure_ascii=False)))
    db.commit()


def drop(g):
    GAMES.pop(g["chat"], None)
    db.execute("DELETE FROM games WHERE chat = ?", (g["chat"],))
    db.commit()


def kv_get(k, default=None):
    row = db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v):
    db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, str(v)))
    db.commit()


# ---------- помощники ----------

def user_name(user):
    n = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    return (n or user.get("username") or "Игрок")[:32]


def h(g, uid):
    """Имя игрока для HTML-сообщения."""
    p = g.get("players", {}).get(uid)
    return html.escape(p["name"] if p else g["joined"].get(uid, "?"))


def is_party(g):
    return g.get("kind") == "party"


def game_of_user(uid):
    """Партия Мафии, в которой участвует игрок (в Крокодиле/Табу записи нет)."""
    for g in GAMES.values():
        if not is_party(g) and uid in g["joined"]:
            return g
    return None


def role_name(g, uid):
    return E.ROLE_NAMES[E.role_of(g, uid)]


def mafia_alive(g):
    return E.alive_with(g, E.MAFIA_TEAM)


# ---------- набор ----------

BOT_NAMES = ["Валера", "Жанна", "Аркадий", "Снежана", "Геннадий", "Кристина", "Эдуард", "Анжела",
             "Борис", "Изольда", "Тимофей", "Лариса", "Гоша", "Милана"]


def lobby_text(g):
    names = "\n".join(f"{i}. {'🤖 ' if is_bot(u) else ''}{html.escape(n)}"
                      for i, (u, n) in enumerate(g["joined"].items(), 1)) or "пока никого"
    bots = sum(is_bot(u) for u in g["joined"])
    return (f"🎭 <b>Мафия 18+ — набор игроков</b>\n\n{names}\n\n"
            f"Игроков: {len(g['joined'])}{f' (из них ботов: {bots})' if bots else ''} "
            f"— нужно {E.MIN_PLAYERS}–{E.MAX_PLAYERS}. Не хватает людей — добавьте ботов.\n"
            "Нажмите «Участвую» — откроется личка бота, там нажмите «Старт». "
            "Когда все собрались, открывший набор нажимает «Начать игру».")


def lobby_rows(chat):
    return [[{"text": "🙋 Участвую", "url": f"https://t.me/{BOT}?start=join_{chat}"}],
            [{"text": "🤖 + бот", "callback_data": f"addbot:{chat}"},
             {"text": "🤖 − бот", "callback_data": f"delbot:{chat}"}],
            [{"text": "▶️ Начать игру", "callback_data": f"begin:{chat}"},
             {"text": "Выйти", "callback_data": f"leave:{chat}"}]]


def bot_lobby(cq, chat, add):
    """Добавить или убрать виртуального игрока — может тот, кто открыл набор, или админ."""
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g.get("kind") == "party" or g["status"] != "lobby":
        answer(cq, "Набор уже закрыт")
        return
    if uid != g["creator"] and not is_admin(chat, uid):
        answer(cq, "Ботов добавляет тот, кто открыл набор, или админ группы", alert=True)
        return
    bots = [u for u in g["joined"] if is_bot(u)]
    if add:
        if len(g["joined"]) >= E.MAX_PLAYERS:
            answer(cq, f"Уже {E.MAX_PLAYERS} игроков — больше нельзя", alert=True)
            return
        used = set(g["joined"].values())
        name = next((f"{n} (бот)" for n in BOT_NAMES if f"{n} (бот)" not in used), f"Бот {len(bots) + 1}")
        bid = next(f"b{i}" for i in range(1, 100) if f"b{i}" not in g["joined"])
        g["joined"][bid] = name
        answer(cq, f"Добавлен {name}")
    else:
        if not bots:
            answer(cq, "Ботов в списке нет")
            return
        del g["joined"][bots[-1]]
        answer(cq, "Бот убран")
    save(g)
    edit(chat, g["msg"], lobby_text(g), lobby_rows(chat))


def open_lobby(chat, title, user):
    if chat in GAMES:
        send(chat, "Здесь уже идёт партия или открыт набор. /stop — прервать.")
        return
    g = {"chat": chat, "title": title, "creator": str(user["id"]), "status": "lobby", "joined": {},
         "deadline": now() + LOBBY_SEC, "night_no": 0, "day_no": 0}
    m = send(chat, lobby_text(g), lobby_rows(chat))
    g["msg"] = m and m["message_id"]
    GAMES[chat] = g
    save(g)


def join(chat, user):
    uid = str(user["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "lobby":
        send(uid, "Набор в эту игру уже закрыт. Попросите открыть новый: /mafia в группе.")
        return
    other = game_of_user(uid)
    if other and other is not g:
        send(uid, "Вы уже участвуете в другой партии.")
        return
    if uid in g["joined"]:
        send(uid, f"Вы уже в списке игры «{html.escape(g['title'])}». Ждите начала.")
        return
    if len(g["joined"]) >= E.MAX_PLAYERS:
        send(uid, f"Мест нет: уже {E.MAX_PLAYERS} игроков.")
        return
    g["joined"][uid] = user_name(user)
    save(g)
    send(uid, f"✅ Вы в игре «{html.escape(g['title'])}». Роль придёт сюда, когда начнётся партия. "
              "Не удаляйте и не блокируйте этот чат.")
    edit(chat, g["msg"], lobby_text(g), lobby_rows(chat))


def leave(cq, chat):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "lobby" or uid not in g["joined"]:
        answer(cq, "Вас нет в списке")
        return
    del g["joined"][uid]
    save(g)
    answer(cq, "Вы вышли из списка")
    edit(chat, g["msg"], lobby_text(g), lobby_rows(chat))


def begin(cq, chat):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "lobby":
        answer(cq, "Набор уже закрыт")
        return
    if uid != g["creator"] and not is_admin(chat, uid):
        answer(cq, "Начать может тот, кто открыл набор, или админ группы", alert=True)
        return
    n = len(g["joined"])
    if n < E.MIN_PLAYERS:
        answer(cq, f"Нужно минимум {E.MIN_PLAYERS} игроков, сейчас {n}. Не хватает людей — добавьте ботов", alert=True)
        return
    if all(is_bot(u) for u in g["joined"]):
        answer(cq, "Нужен хотя бы один живой игрок — нажмите «Участвую»", alert=True)
        return
    answer(cq, "Поехали!")
    g.update(E.new_game(g["joined"]))
    roles = Counter(p["role"] for p in g["players"].values())
    lineup = ", ".join(f"{E.ROLE_NAMES[r]}{f' ×{c}' if c > 1 else ''}" for r, c in roles.items())
    edit(chat, g["msg"], f"🎭 <b>Мафия 18+ — игра началась!</b>\n\nИгроков: {n}.\nВ городе: {lineup}.\n"
                         "Роли пришли в личку. Мёртвые молчат.")
    failed = []
    for u, p in g["players"].items():
        text = (f"Ваша роль в игре «{html.escape(g['title'])}»:\n\n{T.ROLE_INFO[p['role']]}\n\n"
                f"{T.TEAM_GOAL.get(p['role'], T.TOWN_GOAL)}")
        mates = []
        if E.is_mafia(g, u):
            mates = [v for v in mafia_alive(g) if v != u]
            names = [f"{h(g, v)} ({role_name(g, v)})" for v in mates]
            text += ("\n\nВаша семья: " + ", ".join(names) if names else "\n\nВы работаете в одиночку.")
            text += "\nВсё, что вы пишете сюда, увидят остальные мафиози."
        if p["role"] == E.WIFE and g.get("husband"):
            text += f"\n\n💍 Ваш муж: <b>{h(g, g['husband'])}</b>. Об этом знаете только вы."
        text += "\n\nКнопка «🎭 Моя роль» внизу — ваша карточка, ночной ход и тайное голосование."
        card = lambda p=p, mates=mates: C.role_card(  # noqa: E731 — картинка рисуется внутри send_photo
            p["role"], E.ROLE_NAMES[p["role"]], T.ROLE_PLAIN[p["role"]], T.TEAM_GOAL.get(p["role"], T.TOWN_GOAL),
            g["title"], [g["players"][v]["name"] for v in mates])
        if send_photo(u, card, text, markup=role_keyboard(g, u)) is None:
            failed.append(h(g, u))
    if failed:
        send(chat, "⚠️ Не смог написать: " + ", ".join(failed) + ". Откройте личку бота и нажмите «Старт».")
    start_night(g)


# ---------- ночь ----------

def target_rows(g, prefix, targets):
    btns = [{"text": g["players"][t]["name"], "callback_data": f"{prefix}:{t}"} for t in targets]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def b64(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def role_keyboard(g, uid):
    """Постоянная кнопка «🎭 Моя роль» в личке. Всё нужное странице — прямо в ссылке:
    ПК в интернет не открываем, ход возвращается боту через Telegram (sendData)."""
    p = g["players"][uid]
    data = {"c": g["chat"], "g": g["title"][:40], "r": p["role"], "n": g.get("night_no", 0),
            "m": [g["players"][v]["name"][:20] for v in mafia_alive(g) if v != uid] if E.is_mafia(g, uid) else [],
            "p": {u: [q["name"][:20], int(q["alive"])] for u, q in g["players"].items()}}
    if p["role"] == E.WIFE and g.get("husband"):
        data["hb"] = g["players"][g["husband"]]["name"][:20]
    if g["status"] == "night":
        data["t"] = {k: ts for u, k, ts in E.night_tasks(g) if u == uid}
        data["dl"] = int(g["deadline"])
    elif g["status"] == "vote" and uid in E.voters(g):
        data["v"] = [t for t in E.alive(g) if t != uid]
        data["d"] = g["day_no"]
        data["dl"] = int(g["deadline"])
    url = f"{MAFIA_URL}?v={APP_VER}&d={b64(data)}"
    return {"keyboard": [[{"text": "🎭 Моя роль", "web_app": {"url": url}}]],
            "resize_keyboard": True, "is_persistent": True}


def start_night(g):
    E.start_night(g)
    g["status"] = "night"
    g["night_no"] += 1
    g["deadline"] = now() + NIGHT_SEC
    g["prompts"] = {}
    g["bots_at"] = now() + random.uniform(15, min(35, NIGHT_SEC - 5)) if bots_alive(g) else None
    save(g)
    send_photo(g["chat"], lambda: C.night(g["night_no"], len(E.alive(g))),
               T.pick(T.NIGHT, n=g["night_no"]) + f"\n\nНа ход — {NIGHT_SEC} с.")
    tasks = E.night_tasks(g)
    for u in dict.fromkeys(t[0] for t in tasks):  # обновить кнопку «Моя роль»: в ней ночной выбор
        send(u, f"🌙 Ночь {g['night_no']} — ваш ход. Удобнее всего — кнопка «🎭 Моя роль» внизу. "
                "Или выберите кнопками ниже.", markup=role_keyboard(g, u))
    for u, kind, targets in tasks:
        m = send(u, T.PROMPTS[kind].format(n=g["night_no"]), target_rows(g, f"n:{g['chat']}:{kind}", targets))
        if m:
            g["prompts"][f"{u}:{kind}"] = m["message_id"]
    save(g)


def apply_action(g, uid, kind, target):
    """Ночной ход из кнопки в чате или из мини-приложения. Возвращает (принят ли, текст для игрока)."""
    if not g or g["status"] != "night":
        return False, "Ночь уже закончилась"
    if f"{uid}:{kind}" in g["night"]["acts"]:
        return False, "Выбор уже сделан"
    if not E.record_action(g, uid, kind, target):
        return False, "Этот выбор сейчас недоступен"
    edit(uid, g["prompts"].get(f"{uid}:{kind}"),
         T.PROMPTS[kind].format(n=g["night_no"]) + f"\n\nВыбор: <b>{h(g, target)}</b>")
    if kind == E.KILL:
        for v in mafia_alive(g):
            if v != uid:
                send(v, f"🔪 {h(g, uid)} голосует за <b>{h(g, target)}</b>")
    save(g)
    return True, "Принято"


def on_night_action(cq, chat, kind, target):
    g = GAMES.get(chat)
    ok, msg = apply_action(g, str(cq["from"]["id"]), kind, target)
    answer(cq, msg, alert=not ok and msg.startswith("Этот"))
    if ok:
        night_progress(g)


def on_webapp(uid, raw):
    """Ход из мини-приложения «Моя роль»: Telegram присылает его боту как сообщение web_app_data."""
    try:
        data = json.loads(raw)
        g = GAMES.get(str(data["c"]))
        if data.get("a") == "v":
            return webapp_vote(g, uid, data)
        acts = dict(data["acts"])
    except (ValueError, KeyError, TypeError):
        send(uid, "Не понял ход из приложения. Выберите кнопками в чате.")
        return
    if not g or g.get("night_no") != data.get("n"):
        send(uid, "Эта ночь уже закончилась.")
        return
    report = []
    for kind, target in acts.items():
        ok, msg = apply_action(g, uid, str(kind), str(target))
        what = {"kill": "убить", "doncheck": "проверить", "heal": "лечить", "check": "проверить",
                "visit": "навестить", "maniac": "навестить с топором", "watch": "подглядеть за",
                "dance": "станцевать для", "booze": "подпоить"}.get(kind, kind)
        report.append(f"{'✅' if ok else '⚠️'} {what}: <b>{h(g, str(target)) if str(target) in g['players'] else '?'}</b>"
                      + ("" if ok else f" — {msg.lower()}"))
    send(uid, "Ход из приложения:\n" + "\n".join(report))
    if g["status"] == "night":
        night_progress(g)


def finish_night(g):
    chat = g["chat"]
    for key, mid in g.get("prompts", {}).items():
        if key not in g["night"]["acts"]:
            edit(key.split(":")[0], mid, "⏰ Время вышло, ход пропущен.")
    res = E.resolve_night(g)
    for actor, target, kind, ok in res["checks"]:
        if is_bot(actor) and ok and kind == E.CHECK:  # комиссар-бот запоминает найденную мафию
            g.setdefault("botmem", {}).setdefault(actor, {})["found"] = target
        if kind == E.CHECK:
            send(actor, f"🕵️ {h(g, target)} — " + ("<b>МАФИЯ!</b>" if ok else "не мафия."))
        else:
            send(actor, f"🎩 {h(g, target)} — " + ("<b>КОМИССАР!</b>" if ok else "не комиссар."))
    if res["blocked"] and g["players"][res["blocked"]]["alive"]:
        send(res["blocked"], T.BLOCKED)
    if res["watch"]:
        voyeur, target, seen = res["watch"]
        send(voyeur, f"👀 Подглядывая за <b>{h(g, target)}</b>, вы видели: "
                     + (", ".join(f"<b>{h(g, v)}</b>" for v in seen) if seen else "никто не приходил."))
    if res["silenced"]:
        send(res["silenced"], T.DANCED)
    lines = [T.JEALOUS.format(name=h(g, d)) if d == res["jealous"]
             else T.pick(T.DEATH, name=h(g, d), role=role_name(g, d)) for d in res["dead"]]
    if not lines:
        lines.append(T.pick(T.SAVED) if res["saved"] else T.pick(T.QUIET))
    if res["silenced"]:
        lines.append(T.SILENCED.format(name=h(g, res["silenced"])))
    send_photo(chat, lambda: C.morning(g["night_no"], g["players"], res["dead"], E.ROLE_NAMES, bool(res["saved"])),
               "\n\n".join(lines))
    for d in res["dead"]:
        set_muted(g, d, True)
        send(d, "☠️ Вас убили этой ночью. Группу читать можно, писать — нет: мёртвые молчат.", markup=REMOVE_KEYBOARD)
    save(g)
    if not check_win(g):
        start_day(g)


# ---------- день ----------

def start_day(g):
    g["status"] = "day"
    g["day_no"] += 1
    g["deadline"] = now() + DAY_SEC
    g["tovote"] = []
    g["bots_at"] = now() + random.uniform(10, 30) if bots_alive(g) else None  # реплика бота в чате
    a = E.alive(g)
    m = send(g["chat"], T.DAY.format(n=g["day_no"], count=len(a), names=", ".join(h(g, u) for u in a), sec=DAY_SEC),
             [[{"text": "🗳 К голосованию", "callback_data": f"tv:{g['chat']}"}]])
    g["day_msg"] = m and m["message_id"]
    save(g)


def on_tovote(cq, chat):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "day":
        answer(cq, "Сейчас не день")
        return
    a = E.voters(g)
    if uid not in a:
        answer(cq, "Мёртвые не голосуют" if uid not in E.alive(g) else "Сегодня вы не голосуете 💃", alert=True)
        return
    if uid not in g["tovote"]:
        g["tovote"].append(uid)
    for b in a:  # боты поддерживают голосование, только когда его попросил человек
        if is_bot(b) and b not in g["tovote"]:
            g["tovote"].append(b)
    need = len(a) // 2 + 1
    if len(g["tovote"]) >= need:
        answer(cq, "Начинаем голосование")
        start_vote(g)
    else:
        answer(cq, f"За голосование {len(g['tovote'])} из {need} нужных")
        save(g)


def vote_text(g):
    """Голосование тайное: в группе видно только, сколько человек уже проголосовало."""
    return (f"🗳 <b>Тайное голосование.</b> Кого казним?\n\n"
            f"Голосуйте в «🎭 Моя роль» в личке бота (или кнопками ниже — ваш выбор никто не увидит).\n\n"
            f"Проголосовали <b>{len(g['votes'])} из {len(E.voters(g))}</b>. На голосование — {VOTE_SEC} с.")


def tally_text(g):
    """Итог без имён голосовавших: за кого сколько голосов."""
    count = E.tally(g)
    lines = [f"{'🤷 Пропустить' if t == E.SKIP else h(g, t)} — {c}" for t, c in count.most_common()]
    return "Голоса: " + ("; ".join(lines) if lines else "никто не проголосовал")


def vote_rows(g):
    rows = target_rows(g, f"v:{g['chat']}", E.alive(g))
    rows.append([{"text": "🤷 Пропустить", "callback_data": f"v:{g['chat']}:{E.SKIP}"}])
    return rows


def start_vote(g):
    if g["status"] == "day" and g.get("bots_at"):
        bot_chatter(g)  # день оборвали раньше — бот всё равно успевает высказаться
    if g.get("day_msg"):
        safe("editMessageReplyMarkup", chat_id=g["chat"], message_id=g["day_msg"], reply_markup=kb([]))
    E.start_vote(g)
    g["status"] = "vote"
    g["deadline"] = now() + VOTE_SEC
    g["bots_at"] = now() + random.uniform(10, min(25, VOTE_SEC - 5)) if bots_alive(g) else None
    m = send(g["chat"], vote_text(g), vote_rows(g))
    g["vote_msg"] = m and m["message_id"]
    save(g)
    for u in E.voters(g):  # обновить кнопку «Моя роль»: в ней тайное голосование
        send(u, f"🗳 День {g['day_no']}: тайное голосование. Откройте «🎭 Моя роль» внизу.", markup=role_keyboard(g, u))


def on_vote(cq, chat, target):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "vote":
        answer(cq, "Голосование закрыто")
        return
    ok, msg = cast_vote(g, uid, target)
    answer(cq, msg, alert=not ok)


def cast_vote(g, uid, target):
    """Голос из кнопки в группе или из мини-приложения. Возвращает (принят ли, текст для игрока)."""
    if uid not in E.alive(g):
        return False, "Голосуют только живые игроки"
    if uid not in E.voters(g):
        return False, "Сегодня вы не голосуете — после танца Стриптизёрши 💃"
    if not E.record_vote(g, uid, target):
        return False, "За себя голосовать нельзя"
    save(g)
    if g.get("bots_at") and all(v in g["votes"] for v in E.voters(g) if not is_bot(v)):
        bots_vote(g)  # люди проголосовали — боты не тянут время
        return True, "Голос принят. Его никто не увидит"
    if E.vote_done(g):
        finish_vote(g)
    else:
        edit(g["chat"], g["vote_msg"], vote_text(g), vote_rows(g))
    return True, "Голос принят. Его никто не увидит"


def webapp_vote(g, uid, data):
    if not g or g["status"] != "vote" or g.get("day_no") != data.get("d"):
        send(uid, "Голосование уже закрыто.")
        return
    target = str(data.get("t"))
    ok, msg = cast_vote(g, uid, target)
    who = "пропустить" if target == E.SKIP else (h(g, target) if target in g["players"] else "?")
    send(uid, f"{'✅' if ok else '⚠️'} Ваш голос: <b>{who}</b>" + ("" if ok else f" — {msg.lower()}"))


def finish_vote(g):
    summary = tally_text(g)
    edit(g["chat"], g.get("vote_msg"), f"🗳 <b>Голосование закрыто.</b>\n\n{summary}")
    x = E.resolve_vote(g)
    caption = T.pick(T.EXECUTED, name=h(g, x), role=role_name(g, x)) if x else T.pick(T.NO_EXECUTION)
    caption += f"\n\n{summary}"
    send_photo(g["chat"], lambda: C.verdict(g["players"], x, E.ROLE_NAMES), caption)
    if x:
        set_muted(g, x, True)
        send(x, "⚖️ Вас казнили. Группу читать можно, писать — нет: мёртвые молчат.", markup=REMOVE_KEYBOARD)
    save(g)
    if not check_win(g):
        start_night(g)


# ---------- боты-игроки ----------

BOT_CHATTER = ["Я мирный, честно-честно.", "Что-то {x} подозрительно молчит…", "{x}, а ты где был ночью?",
               "Голосую за {x}, интуиция подсказывает.", "Мне кажется, мафия среди тех, кто громче всех шутит.",
               "Давайте не торопиться, у меня алиби: я спал.", "{x} слишком хорошо всё объясняет. Не верю."]


def bots_alive(g):
    return [u for u in E.alive(g) if is_bot(u)]


def bot_target(g, b, kind, targets):
    """Ход бота ночью: мафия поддерживает выбор семьи, комиссар не проверяет одних и тех же."""
    if kind == E.KILL:
        votes = [t for k, t in g["night"]["acts"].items() if k.endswith(":" + E.KILL) and t in targets]
        if votes:
            return Counter(votes).most_common(1)[0][0]
    mem = g.setdefault("botmem", {}).setdefault(b, {})
    if kind in (E.CHECK, E.DON_CHECK):
        fresh = [t for t in targets if t not in mem.get("checked", [])]
        t = random.choice(fresh or targets)
        mem.setdefault("checked", []).append(t)
        return t
    if kind == E.HEAL and b in targets and random.random() < .3:
        return b
    return random.choice(targets)


def bots_night(g):
    g["bots_at"] = None
    for u, kind, targets in E.night_tasks(g):
        if is_bot(u) and f"{u}:{kind}" not in g["night"]["acts"]:
            apply_action(g, u, kind, bot_target(g, u, kind, targets))
    save(g)


def night_progress(g):
    """После хода человека: если все люди походили — боты ходят сразу; все походили — утро."""
    if g.get("bots_at") and all(f"{u}:{k}" in g["night"]["acts"] for u, k, _ in E.night_tasks(g) if not is_bot(u)):
        bots_night(g)
    if g["status"] == "night" and E.night_done(g):
        finish_night(g)


def bot_vote_target(g, b):
    a = [t for t in E.alive(g) if t != b]
    found = g.get("botmem", {}).get(b, {}).get("found")
    if found in a:
        return found
    count = Counter(t for t in g["votes"].values() if t != E.SKIP and t in a)
    if E.is_mafia(g, b):
        pool = [t for t in a if not E.is_mafia(g, t)] or a
        lead = [t for t, _ in count.most_common() if t in pool]
        return lead[0] if lead and random.random() < .7 else random.choice(pool)
    if count and random.random() < .5:
        return count.most_common(1)[0][0]
    return E.SKIP if random.random() < .15 else random.choice(a)


def bots_vote(g):
    g["bots_at"] = None
    for b in E.voters(g):
        if is_bot(b) and b not in g["votes"]:
            E.record_vote(g, b, bot_vote_target(g, b))
    save(g)
    if E.vote_done(g):
        finish_vote(g)
    else:
        edit(g["chat"], g.get("vote_msg"), vote_text(g), vote_rows(g))


def bot_chatter(g):
    g["bots_at"] = None
    bots = bots_alive(g)
    if bots:
        b = random.choice(bots)
        found = g.get("botmem", {}).get(b, {}).get("found")
        others = [t for t in E.alive(g) if t != b]
        line = (f"Я кое-что знаю: {g['players'][found]['name']} — мафия! Голосуйте за него."
                if found in others else random.choice(BOT_CHATTER).format(x=g["players"][random.choice(others)]["name"]))
        send(g["chat"], f"🤖 <b>{h(g, b)}</b>: {html.escape(line)}")
    save(g)


# ---------- конец ----------

def roles_list(g):
    return "\n".join(f"{'🙂' if p['alive'] else '💀'} {h(g, u)} — {E.ROLE_NAMES[p['role']]}"
                     for u, p in g["players"].items())


def check_win(g):
    w = E.winner(g)
    if w:
        end_game(g, w)
    return bool(w)


def end_game(g, result):
    """result — 'town' | 'mafia' | 'maniac' | 'draw' | 'stop'."""
    headline = T.WIN.get(result, "⛔ <b>Партия остановлена.</b>")
    if result != "stop" and E.wife_won(g):
        wife = next(u for u, p in g["players"].items() if p["role"] == E.WIFE)
        headline += "\n\n" + T.WIFE_WIN.format(name=h(g, wife))
    send_photo(g["chat"], lambda: C.final(g["players"], result, E.ROLE_NAMES),
               f"{headline}\n\n<b>Роли:</b>\n{roles_list(g)}\n\nЕщё партию? /mafia")
    for u in g.get("restricted", []):
        set_muted(g, u, False)
    for u, p in g["players"].items():
        if p["alive"]:  # у мёртвых кнопку уже убрали
            send(u, f"🏁 Игра в «{html.escape(g['title'])}» окончена. Итоги — в группе.", markup=REMOVE_KEYBOARD)
    drop(g)


def stop(chat, user):
    g = GAMES.get(chat)
    if not g:
        send(chat, "Сейчас здесь нет игры. Начать: /mafia, /krokodil или /tabu")
        return
    uid = str(user["id"])
    if uid != g["creator"] and not is_admin(chat, uid):
        send(chat, "Прервать может тот, кто начал игру, или админ группы.")
        return
    if is_party(g):
        party_end(g, "🏁 <b>Игра окончена.</b>")
    elif g["status"] == "lobby":
        edit(chat, g["msg"], "🎭 Набор в Мафию отменён.")
        drop(g)
        send(chat, "Набор отменён.")
    else:
        end_game(g, "stop")


# ---------- Крокодил и Табу в группе ----------

PARTY_NAMES = {"croc": "🐊 Крокодил", "tabu": "🚫 Табу"}
PARTY_HOW = {
    "croc": "Ведущий объясняет слово <b>текстом</b>, но не называет его и однокоренные.",
    "tabu": "Ведущий объясняет слово <b>текстом</b>, но не называет его, однокоренные и <b>5 запретных слов</b> с карточки.",
}


def party_open(chat, title, user, mode):
    global DECKS
    g = GAMES.get(chat)
    if g:
        busy = PARTY_NAMES[g["mode"]] if is_party(g) else "Мафия"
        send(chat, f"Здесь уже идёт игра: {busy}. /stop — закончить её.")
        return
    if DECKS is None:
        DECKS = P.load_decks()
    g = {"chat": chat, "title": title, "kind": "party", "mode": mode, "creator": str(user["id"]),
         "status": "idle", "leader": None, "card": None, "used": [], "scores": {}, "msg": None,
         "deadline": now() + PARTY_IDLE_SEC}
    GAMES[chat] = g
    send(chat, f"<b>{PARTY_NAMES[mode]} в чате!</b>\n\n{PARTY_HOW[mode]} "
               "Остальные пишут догадки прямо в чат — бот сам засчитает правильную.\n\n"
               f"Угадал — +1 тебе и +1 ведущему, и ведёшь ты. На слово {PARTY_TURN_SEC} с. "
               f"Нарушение ведущего — −1.\n/score — счёт, /stop — закончить.")
    party_ask_leader(g)


def party_ask_leader(g, prefix=""):
    g["status"], g["leader"], g["card"] = "idle", None, None
    g["deadline"] = now() + PARTY_IDLE_SEC
    m = send(g["chat"], prefix + "🎙 Кто ведёт?", [[{"text": "🙋 Хочу вести", "callback_data": f"pl:{g['chat']}"}]])
    g["msg"] = m and m["message_id"]
    save(g)


def party_add(g, uid, name, points):
    s = g["scores"].setdefault(uid, {"name": name, "score": 0})
    s["name"] = name
    s["score"] += points


def party_leader_rows(chat):
    return [[{"text": "👀 Посмотреть слово", "callback_data": f"pw:{chat}"}],
            [{"text": "🔄 Другое слово", "callback_data": f"pn:{chat}"},
             {"text": "🏳 Передать ход", "callback_data": f"pp:{chat}"}]]


def party_start_turn(g, uid, name):
    if g.get("msg"):
        safe("editMessageReplyMarkup", chat_id=g["chat"], message_id=g["msg"], reply_markup=kb([]))
    g["status"], g["leader"], g["leader_name"] = "turn", uid, name
    g["card"] = P.draw(DECKS[g["mode"]], g["used"])
    g["deadline"] = now() + PARTY_TURN_SEC
    party_add(g, uid, name, 0)
    m = send(g["chat"], f"🎙 Ведёт <b>{html.escape(name)}</b>. Жми «Посмотреть слово» — его увидишь только ты. "
                        f"Остальные — угадывайте! ({PARTY_TURN_SEC} с)", party_leader_rows(g["chat"]))
    g["msg"] = m and m["message_id"]
    save(g)


def party_card_text(g):
    c = g["card"]
    text = f"Слово: {c['t']}\n({c['c']})"
    if g["mode"] == "tabu":
        text += "\n\nНельзя: " + ", ".join(c["x"])
    return text + "\n\nОбъясняй текстом, не называя слово."


def party_callback(cq, kind, chat):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or not is_party(g):
        answer(cq, "Игра уже закончилась")
        return
    if kind == "pl":
        if g["status"] != "idle":
            answer(cq, "Ведущий уже есть")
            return
        answer(cq, "Ты ведёшь! Жми «Посмотреть слово»")
        party_start_turn(g, uid, user_name(cq["from"]))
        return
    if g["status"] != "turn" or uid != g["leader"]:
        answer(cq, "Слово видит только ведущий 🙂", alert=True)
        return
    if kind == "pw":
        answer(cq, party_card_text(g)[:200], alert=True)  # всплывающее окно Telegram — до 200 символов
    elif kind == "pn":
        g["card"] = P.draw(DECKS[g["mode"]], g["used"])
        save(g)
        answer(cq, ("🔄 Новое. " + party_card_text(g))[:200], alert=True)
    elif kind == "pp":
        answer(cq, "Ход передан")
        word = html.escape(g["card"]["t"])
        party_ask_leader(g, f"🏳 {html.escape(g['leader_name'])} передаёт ход. Слово было: <b>{word}</b>\n\n")


def party_text(g, m):
    """Сообщение в чате во время хода: догадка игрока или проверка ведущего на нарушения."""
    if g["status"] != "turn":
        return
    user = m.get("from") or {}
    uid, name = str(user.get("id")), user_name(user)
    text = m.get("text") or ""
    card = g["card"]
    if uid == g["leader"]:
        bad = P.violations(card, g["mode"], text)
        if bad:
            party_add(g, uid, name, -1)
            g["card"] = P.draw(DECKS[g["mode"]], g["used"])
            save(g)
            send(g["chat"], f"🚫 Ведущий сказал запретное ({html.escape(', '.join(bad))}). −1. "
                            f"Слово было: <b>{html.escape(card['t'])}</b>. Ведущий, жми «Посмотреть слово» — оно новое.")
        return
    if P.guessed(card["t"], text):
        party_add(g, uid, name, 1)
        party_add(g, g["leader"], g["leader_name"], 1)
        send(g["chat"], f"✅ <b>{html.escape(name)}</b> угадал(а): <b>{html.escape(card['t'])}</b>! "
                        f"+1 угадавшему и +1 ведущему ({html.escape(g['leader_name'])}).")
        party_start_turn(g, uid, name)


def party_score_text(g):
    rows = sorted(g["scores"].values(), key=lambda s: -s["score"])
    if not rows:
        return "Пока никто не набрал очков."
    medals = ["🥇", "🥈", "🥉"]
    return "\n".join(f"{medals[i] if i < 3 else '▫️'} {html.escape(s['name'])} — {s['score']}" for i, s in enumerate(rows))


def party_timeout(g):
    if g["status"] == "turn":
        party_ask_leader(g, f"⏰ Время вышло. Слово было: <b>{html.escape(g['card']['t'])}</b>\n\n")
    else:
        party_end(g, "💤 Давно никто не ведёт — игра закрыта.")


def party_end(g, headline):
    if g.get("msg"):
        safe("editMessageReplyMarkup", chat_id=g["chat"], message_id=g["msg"], reply_markup=kb([]))
    send(g["chat"], f"{headline}\n\n<b>Счёт — {PARTY_NAMES[g['mode']]}:</b>\n{party_score_text(g)}\n\n"
                    "Ещё? /krokodil, /tabu или /mafia")
    drop(g)


# ---------- меню игр в группе ----------

MENU_TEXT = ("🎲 <b>Во что играем?</b>\n\n"
             "🎭 <b>Мафия 18+</b> — у каждого свой телефон: роль в личку, ночью ход в приложении, днём голосуем. От 5 человек.\n"
             "🐊 <b>Крокодил</b> и 🚫 <b>Табу</b> — один телефон по кругу: карточки, таймер, счёт команд." + T.DEV)

_app_check = [0.0, False]  # когда проверяли и включено ли мини-приложение бота в BotFather


def app_link(mode):
    """Ссылка на игру на одном телефоне сразу в нужном режиме.

    Если в BotFather включено мини-приложение бота — откроется внутри Telegram (startapp),
    иначе — та же игра в браузере. Проверяем раз в 5 минут, перезапуск не нужен.
    """
    if now() - _app_check[0] > 300:
        me = safe("getMe") or {}
        _app_check[:] = [now(), bool(me.get("has_main_web_app"))]
    return f"https://t.me/{BOT}?startapp={mode}" if _app_check[1] else f"{APP_URL}?mode={mode}"


def menu_rows():
    return [[{"text": "🎭 Мафия 18+", "callback_data": "gm:mafia"}],
            [{"text": "🐊 Крокодил", "url": app_link("croc")},
             {"text": "🚫 Табу", "url": app_link("tabu")}],
            [{"text": "📜 Правила", "callback_data": "gm:rules"}]]


def send_app_button(chat, mode):
    name = {"croc": "🐊 Крокодил", "tabu": "🚫 Табу"}[mode]
    send(chat, f"<b>{name}</b> — играем на одном телефоне, передавая его по кругу: карточки, таймер, счёт команд.",
         [[{"text": f"▶️ Открыть {name[2:]}", "url": app_link(mode)}]])


def games_menu(chat):
    """Меню с кнопками игр; если у бота есть право — закрепляет его вместо прошлого меню."""
    m = send(chat, MENU_TEXT, menu_rows())
    if not m:
        return
    old = kv_get(f"menu:{chat}")
    if old:
        safe("unpinChatMessage", chat_id=chat, message_id=int(old))
    if safe("pinChatMessage", chat_id=chat, message_id=m["message_id"], disable_notification=True):
        kv_set(f"menu:{chat}", m["message_id"])


def menu_callback(cq, game):
    chat = cq["message"]["chat"]
    cid, title, user = str(chat["id"]), chat.get("title") or "Игра", cq["from"]
    answer(cq)
    if game == "mafia":
        open_lobby(cid, title, user)
    elif game in ("croc", "tabu"):  # кнопки из старых закреплённых меню
        send_app_button(cid, game)
    elif game == "rules":
        send(cid, T.RULES + "\n\nКрокодил и Табу открываются кнопками меню: играете на одном телефоне, "
                            "передавая его по кругу." + T.DEV)


# ---------- таймеры ----------

def tick():
    for g in list(GAMES.values()):
        if g.get("bots_at") and now() >= g["bots_at"] and not is_party(g):
            try:
                if g["status"] == "night":
                    bots_night(g)
                    if E.night_done(g):
                        finish_night(g)
                elif g["status"] == "day":
                    bot_chatter(g)
                elif g["status"] == "vote":
                    bots_vote(g)
            except Exception:
                log("боты: " + traceback.format_exc())
                g["bots_at"] = None
            if g["chat"] not in GAMES:
                continue
        if g["deadline"] > now():
            continue
        try:
            st = g["status"]
            if is_party(g):
                party_timeout(g)
            elif st == "lobby":
                edit(g["chat"], g["msg"], "🎭 Набор закрыт: за 30 минут игру так и не начали. /mafia — открыть снова.")
                drop(g)
            elif st == "night":
                finish_night(g)
            elif st == "day":
                start_vote(g)
            elif st == "vote":
                finish_vote(g)
        except Exception:
            log("tick: " + traceback.format_exc())
            g["deadline"] = now() + 30  # не крутить ошибку каждую секунду


def next_wait():
    if not GAMES:
        return 25
    nearest = min(min(g["deadline"], g.get("bots_at") or g["deadline"]) for g in GAMES.values())
    return max(1, min(25, int(nearest - now()) + 1))


# ---------- входящие ----------

GREETING = ("👋 Я веду <b>Мафию 18+</b> в группах, где у каждого свой телефон, "
            "и открываю <b>Крокодил</b> и <b>Табу</b> — их играют на одном телефоне по кругу.\n\n"
            "Добавьте меня в группу — я пришлю меню с кнопками игр (или /games). "
            "Дайте мне право закреплять сообщения — закреплю меню вверху чата. Правила Мафии — /rules.\n\n"
            "Играть всем на одном телефоне — кнопка «Играть» внизу." + T.DEV)


def on_message(m):
    text = (m.get("text") or "").strip()
    chat = m["chat"]
    user = m.get("from") or {}
    if chat["type"] == "private":
        uid = str(chat["id"])
        if text.startswith("/start"):
            arg = text.split(maxsplit=1)[1] if " " in text else ""
            if arg.startswith("join_"):
                join(arg[5:], user)
            else:
                send(uid, GREETING)
        elif text.startswith("/rules"):
            send(uid, T.RULES + T.DEV)
        elif m.get("web_app_data"):
            on_webapp(uid, m["web_app_data"].get("data", ""))
        elif text:
            relay_mafia(uid, text)
        return
    cid = str(chat["id"])
    if any((u.get("username") or "").lower() == BOT.lower() for u in m.get("new_chat_members", [])):
        games_menu(cid)  # бота только что добавили в группу
        return
    if not text.startswith("/"):
        g = GAMES.get(cid)
        if g and is_party(g):
            party_text(g, m)
        elif g and g["status"] != "lobby" and str(user.get("id")) in g.get("players", {}) \
                and not g["players"][str(user.get("id"))]["alive"]:
            safe("deleteMessage", chat_id=cid, message_id=m["message_id"])  # мёртвые молчат (если бот админ)
        return
    head = text.split()[0]
    cmd, _, target = head.partition("@")
    if target and target.lower() != BOT.lower():
        return  # команда другому боту
    cmd = cmd.lower()
    title = chat.get("title") or "Игра"
    if cmd in ("/games", "/menu"):
        games_menu(cid)
    elif cmd == "/mafia":
        open_lobby(cid, title, user)
    elif cmd in ("/krokodil", "/croc"):
        send_app_button(cid, "croc")
    elif cmd == "/tabu":
        send_app_button(cid, "tabu")
    elif cmd == "/krokodil_chat":  # запасной текстовый режим: объясняют в чате, бот ловит догадки
        party_open(cid, title, user, "croc")
    elif cmd == "/tabu_chat":
        party_open(cid, title, user, "tabu")
    elif cmd == "/score":
        g = GAMES.get(cid)
        send(cid, party_score_text(g) if g and is_party(g) else "Счёт ведётся в Крокодиле и Табу: /krokodil или /tabu")
    elif cmd == "/stop":
        stop(cid, user)
    elif cmd in ("/rules", "/start"):
        send(cid, T.RULES + "\n\nЕщё игры: /krokodil и /tabu — на одном телефоне по кругу. "
                            "Вариант в чате (бот сам ловит догадки): /krokodil_chat, /tabu_chat." + T.DEV)


def relay_mafia(uid, text):
    """Тайный чат мафии: сообщение члена семьи в личку бота уходит остальным мафиози."""
    g = game_of_user(uid)
    if g and g["status"] != "lobby" and uid in g["players"] and g["players"][uid]["alive"] and E.is_mafia(g, uid):
        mates = [v for v in mafia_alive(g) if v != uid]
        if not mates:
            send(uid, "В семье вы один — переписываться не с кем.")
        for v in mates:
            send(v, f"🔪 <b>{h(g, uid)}</b>: {html.escape(text)}")
    elif not g:
        send(uid, "Я понимаю команды и кнопки. Правила — /rules")


def on_callback(cq):
    parts = (cq.get("data") or "").split(":")
    try:
        kind = parts[0]
        if kind == "begin":
            begin(cq, parts[1])
        elif kind in ("addbot", "delbot"):
            bot_lobby(cq, parts[1], kind == "addbot")
        elif kind == "leave":
            leave(cq, parts[1])
        elif kind == "n":
            on_night_action(cq, parts[1], parts[2], parts[3])
        elif kind == "tv":
            on_tovote(cq, parts[1])
        elif kind == "v":
            on_vote(cq, parts[1], parts[2])
        elif kind in ("pl", "pw", "pn", "pp"):
            party_callback(cq, kind, parts[1])
        elif kind == "gm":
            menu_callback(cq, parts[1])
        else:
            answer(cq)
    except IndexError:
        answer(cq)


def handle(update):
    if "callback_query" in update:
        on_callback(update["callback_query"])
    elif "message" in update:
        on_message(update["message"])


# ---------- Windows: один экземпляр и запрет сна ----------

_mutex = None


def single_instance():
    global _mutex
    if sys.platform != "win32":
        return True
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\KrokodilMafiaBot")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


_awake = None


def set_awake(on):
    """Пока идёт партия, ПК не уходит в сон. Настройки электропитания не меняются."""
    global _awake
    if on == _awake or sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    _awake = on
    log("запрет сна " + ("включён" if on else "выключен"))


# ---------- главный цикл ----------

def resume_after_downtime():
    last = float(kv_get("heartbeat", 0))
    if now() - last < 90:
        return
    for g in GAMES.values():
        if g["status"] not in ("lobby", "idle"):
            g["deadline"] = max(g["deadline"], now() + RESUME_GRACE)
            save(g)
            send(g["chat"], "🔌 Бот был недоступен, теперь вернулся. Продолжаем с того же места.")


def main():
    global API, BOT, DECKS
    if not single_instance():
        return
    API = f"https://api.telegram.org/bot{TOKEN_PATH.read_text(encoding='utf-8-sig').strip()}/"
    DECKS = P.load_decks()
    db_init()
    GAMES.update(load_games())
    while True:
        try:
            BOT = api("getMe")["username"]
            break
        except (TgError, OSError) as e:
            log(f"getMe: {e}")
            time.sleep(15)
    log(f"старт @{BOT}, партий в базе: {len(GAMES)}")
    safe("setMyCommands", scope={"type": "all_group_chats"}, commands=[
        {"command": "games", "description": "Меню игр с кнопками"},
        {"command": "mafia", "description": "Мафия 18+: открыть набор"},
        {"command": "krokodil", "description": "Крокодил: открыть игру (один телефон по кругу)"},
        {"command": "tabu", "description": "Табу: открыть игру (один телефон по кругу)"},
        {"command": "stop", "description": "Закончить игру"},
        {"command": "rules", "description": "Правила"}])
    safe("setMyCommands", scope={"type": "all_private_chats"}, commands=[
        {"command": "rules", "description": "Правила Мафии"}])
    resume_after_downtime()
    offset = int(kv_get("offset", 0))
    while True:
        kv_set("heartbeat", now())
        tick()
        set_awake(any(g["status"] not in ("lobby", "idle") for g in GAMES.values()))
        try:
            updates = api("getUpdates", offset=offset, timeout=next_wait(),
                          allowed_updates=["message", "callback_query"])
        except TgError as e:
            log(f"getUpdates: {e}")
            time.sleep(30 if e.code == 409 else 5)
            continue
        except OSError as e:
            log(f"getUpdates: сеть: {e}")
            time.sleep(5)
            continue
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            kv_set("offset", offset)
            try:
                handle(u)
            except Exception:
                log("handle: " + traceback.format_exc())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal: " + traceback.format_exc())
        raise
