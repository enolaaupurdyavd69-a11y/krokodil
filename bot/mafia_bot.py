"""Мафия 18+ — Telegram-бот для групп, у каждого игрока свой телефон.

Постоянный процесс на ПК: long polling, как у Печкина, — без белого IP и открытых портов.
Запуск: pythonw mafia_bot.py (задача Планировщика KrokodilMafiaBot, см. install_task.ps1).
Токен — bot/token.txt (в .gitignore). База партий и лог — bot/data/.
Партии лежат в SQLite: после перезапуска ПК бот продолжает с той же фазы.
"""
import html
import json
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import mafia_engine as E
import party_engine as P
import texts as T

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


def kb(rows):
    return {"inline_keyboard": rows}


def send(chat, text, rows=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows:
        p["reply_markup"] = kb(rows)
    return safe("sendMessage", **p)


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

def lobby_text(g):
    names = "\n".join(f"{i}. {html.escape(n)}" for i, n in enumerate(g["joined"].values(), 1)) or "пока никого"
    return (f"🎭 <b>Мафия 18+ — набор игроков</b>\n\n{names}\n\n"
            f"Игроков: {len(g['joined'])} (нужно {E.MIN_PLAYERS}–{E.MAX_PLAYERS}).\n"
            "Нажмите «Участвую» — откроется личка бота, там нажмите «Старт». "
            "Когда все собрались, открывший набор нажимает «Начать игру».")


def lobby_rows(chat):
    return [[{"text": "🙋 Участвую", "url": f"https://t.me/{BOT}?start=join_{chat}"}],
            [{"text": "▶️ Начать игру", "callback_data": f"begin:{chat}"},
             {"text": "Выйти", "callback_data": f"leave:{chat}"}]]


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
        answer(cq, f"Нужно минимум {E.MIN_PLAYERS} игроков, сейчас {n}", alert=True)
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
        if E.is_mafia(g, u):
            mates = [f"{h(g, v)} ({role_name(g, v)})" for v in mafia_alive(g) if v != u]
            text += ("\n\nВаша семья: " + ", ".join(mates) if mates else "\n\nВы работаете в одиночку.")
            text += "\nВсё, что вы пишете сюда, увидят остальные мафиози."
        if send(u, text) is None:
            failed.append(h(g, u))
    if failed:
        send(chat, "⚠️ Не смог написать: " + ", ".join(failed) + ". Откройте личку бота и нажмите «Старт».")
    start_night(g)


# ---------- ночь ----------

def target_rows(g, prefix, targets):
    btns = [{"text": g["players"][t]["name"], "callback_data": f"{prefix}:{t}"} for t in targets]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def start_night(g):
    E.start_night(g)
    g["status"] = "night"
    g["night_no"] += 1
    g["deadline"] = now() + NIGHT_SEC
    g["prompts"] = {}
    save(g)
    send(g["chat"], T.pick(T.NIGHT, n=g["night_no"]) + f"\n\nНа ход — {NIGHT_SEC} с.")
    for u, kind, targets in E.night_tasks(g):
        m = send(u, T.PROMPTS[kind].format(n=g["night_no"]), target_rows(g, f"n:{g['chat']}:{kind}", targets))
        if m:
            g["prompts"][f"{u}:{kind}"] = m["message_id"]
    save(g)


def on_night_action(cq, chat, kind, target):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "night":
        answer(cq, "Ночь уже закончилась")
        return
    if f"{uid}:{kind}" in g["night"]["acts"]:
        answer(cq, "Выбор уже сделан")
        return
    if not E.record_action(g, uid, kind, target):
        answer(cq, "Этот выбор сейчас недоступен", alert=True)
        return
    answer(cq, "Принято")
    edit(uid, cq["message"]["message_id"],
         T.PROMPTS[kind].format(n=g["night_no"]) + f"\n\nВыбор: <b>{h(g, target)}</b>")
    if kind == E.KILL:
        for v in mafia_alive(g):
            if v != uid:
                send(v, f"🔪 {h(g, uid)} голосует за <b>{h(g, target)}</b>")
    save(g)
    if E.night_done(g):
        finish_night(g)


def finish_night(g):
    chat = g["chat"]
    for key, mid in g.get("prompts", {}).items():
        if key not in g["night"]["acts"]:
            edit(key.split(":")[0], mid, "⏰ Время вышло, ход пропущен.")
    res = E.resolve_night(g)
    for actor, target, kind, ok in res["checks"]:
        if kind == E.CHECK:
            send(actor, f"🕵️ {h(g, target)} — " + ("<b>МАФИЯ!</b>" if ok else "не мафия."))
        else:
            send(actor, f"🎩 {h(g, target)} — " + ("<b>КОМИССАР!</b>" if ok else "не комиссар."))
    if res["blocked"] and g["players"][res["blocked"]]["alive"]:
        send(res["blocked"], T.BLOCKED)
    lines = [T.pick(T.DEATH, name=h(g, d), role=role_name(g, d)) for d in res["dead"]]
    if not lines:
        lines.append(T.pick(T.SAVED) if res["saved"] else T.pick(T.QUIET))
    send(chat, "\n\n".join(lines))
    for d in res["dead"]:
        set_muted(g, d, True)
        send(d, "☠️ Вас убили этой ночью. Группу читать можно, писать — нет: мёртвые молчат.")
    save(g)
    if not check_win(g):
        start_day(g)


# ---------- день ----------

def start_day(g):
    g["status"] = "day"
    g["day_no"] += 1
    g["deadline"] = now() + DAY_SEC
    g["tovote"] = []
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
    a = E.alive(g)
    if uid not in a:
        answer(cq, "Мёртвые не голосуют", alert=True)
        return
    if uid not in g["tovote"]:
        g["tovote"].append(uid)
    need = len(a) // 2 + 1
    if len(g["tovote"]) >= need:
        answer(cq, "Начинаем голосование")
        start_vote(g)
    else:
        answer(cq, f"За голосование {len(g['tovote'])} из {need} нужных")
        save(g)


def vote_text(g):
    count = E.tally(g)
    voters = {}
    for v, t in g["votes"].items():
        voters.setdefault(t, []).append(h(g, v))
    lines = [f"<b>{'Пропустить' if t == E.SKIP else h(g, t)}</b> — {c}: {', '.join(voters[t])}"
             for t, c in count.most_common()]
    body = "\n".join(lines) or "Пока никто не проголосовал."
    return T.VOTE.format(tally=body) + f"\n\nПроголосовали {len(g['votes'])} из {len(E.alive(g))}. На голосование — {VOTE_SEC} с."


def vote_rows(g):
    rows = target_rows(g, f"v:{g['chat']}", E.alive(g))
    rows.append([{"text": "🤷 Пропустить", "callback_data": f"v:{g['chat']}:{E.SKIP}"}])
    return rows


def start_vote(g):
    if g.get("day_msg"):
        safe("editMessageReplyMarkup", chat_id=g["chat"], message_id=g["day_msg"], reply_markup=kb([]))
    E.start_vote(g)
    g["status"] = "vote"
    g["deadline"] = now() + VOTE_SEC
    m = send(g["chat"], vote_text(g), vote_rows(g))
    g["vote_msg"] = m and m["message_id"]
    save(g)


def on_vote(cq, chat, target):
    uid = str(cq["from"]["id"])
    g = GAMES.get(chat)
    if not g or g["status"] != "vote":
        answer(cq, "Голосование закрыто")
        return
    if uid not in E.alive(g):
        answer(cq, "Голосуют только живые игроки", alert=True)
        return
    if not E.record_vote(g, uid, target):
        answer(cq, "За себя голосовать нельзя", alert=True)
        return
    answer(cq, "Голос принят")
    save(g)
    if E.vote_done(g):
        finish_vote(g)
    else:
        edit(chat, g["vote_msg"], vote_text(g), vote_rows(g))


def finish_vote(g):
    edit(g["chat"], g.get("vote_msg"), vote_text(g).rsplit("\n\n", 1)[0] + "\n\nГолосование закрыто.")
    x = E.resolve_vote(g)
    if x:
        send(g["chat"], T.pick(T.EXECUTED, name=h(g, x), role=role_name(g, x)))
        set_muted(g, x, True)
        send(x, "⚖️ Вас казнили. Группу читать можно, писать — нет: мёртвые молчат.")
    else:
        send(g["chat"], T.pick(T.NO_EXECUTION))
    save(g)
    if not check_win(g):
        start_night(g)


# ---------- конец ----------

def roles_list(g):
    return "\n".join(f"{'🙂' if p['alive'] else '💀'} {h(g, u)} — {E.ROLE_NAMES[p['role']]}"
                     for u, p in g["players"].items())


def check_win(g):
    w = E.winner(g)
    if w:
        end_game(g, T.WIN[w])
    return bool(w)


def end_game(g, headline):
    send(g["chat"], f"{headline}\n\n<b>Роли:</b>\n{roles_list(g)}\n\nЕщё партию? /mafia")
    for u in g.get("restricted", []):
        set_muted(g, u, False)
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
        end_game(g, "⛔ <b>Партия остановлена.</b>")


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
             "🎭 <b>Мафия 18+</b> — роли в личку, ночью ходы кнопками, днём голосуем. От 5 человек.\n"
             "🐊 <b>Крокодил</b> — ведущий объясняет слово текстом, бот сам ловит правильную догадку.\n"
             "🚫 <b>Табу</b> — то же, но нельзя говорить 5 запретных слов." + T.DEV)


def menu_rows():
    return [[{"text": "🎭 Мафия 18+", "callback_data": "gm:mafia"},
             {"text": "🐊 Крокодил", "callback_data": "gm:croc"},
             {"text": "🚫 Табу", "callback_data": "gm:tabu"}],
            [{"text": "📜 Правила", "callback_data": "gm:rules"}]]


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
    elif game in ("croc", "tabu"):
        party_open(cid, title, user, game)
    elif game == "rules":
        send(cid, T.RULES + "\n\nКрокодил и Табу: ведущий жмёт «Посмотреть слово» и объясняет текстом, "
                            "остальные пишут догадки в чат — бот сам засчитывает правильную." + T.DEV)


# ---------- таймеры ----------

def tick():
    for g in list(GAMES.values()):
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
    nearest = min(g["deadline"] for g in GAMES.values())
    return max(1, min(25, int(nearest - now()) + 1))


# ---------- входящие ----------

GREETING = ("👋 Я веду игры в группах, где у каждого свой телефон: <b>Мафия 18+</b> (/mafia), "
            "<b>Крокодил</b> (/krokodil) и <b>Табу</b> (/tabu) — в них я сам засчитываю догадки из чата.\n\n"
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
        party_open(cid, title, user, "croc")
    elif cmd == "/tabu":
        party_open(cid, title, user, "tabu")
    elif cmd == "/score":
        g = GAMES.get(cid)
        send(cid, party_score_text(g) if g and is_party(g) else "Счёт ведётся в Крокодиле и Табу: /krokodil или /tabu")
    elif cmd == "/stop":
        stop(cid, user)
    elif cmd in ("/rules", "/start"):
        send(cid, T.RULES + "\n\nЕщё игры: /krokodil и /tabu — объясняешь слово текстом, бот сам ловит правильные догадки." + T.DEV)


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
        {"command": "krokodil", "description": "Крокодил: объясняй текстом, бот ловит догадки"},
        {"command": "tabu", "description": "Табу: объясняй без 5 запретных слов"},
        {"command": "score", "description": "Счёт в Крокодиле/Табу"},
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
