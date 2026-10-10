"""Симуляция партии Мафии: виртуальные игроки играют через настоящий код бота, Telegram подменён.

Результат — HTML-отчёт: лента группы (тексты и картинки), «взгляд ведущего» по ночам и лички игроков.
Ничего не отправляет в Telegram и не трогает базу работающего бота.

    python -X utf8 simulate.py                 # 12 игроков, случайная партия
    python -X utf8 simulate.py --players 15 --seed 7
"""
import argparse
import base64
import html
import json
import os
import random
import tempfile
from pathlib import Path

import mafia_bot as B
import mafia_engine as E

NAMES = ["Игорь", "Маша", "Дима", "Оля", "Сергей", "Катя", "Антон", "Лёха", "Вика", "Настя", "Паша", "Света",
         "Рома", "Юля", "Костя"]
CHATTER = ["Я мирный, клянусь мамой!", "Кто-то слишком тихий сегодня…", "Мне не нравится, как {x} смотрит.",
           "Предлагаю {x} — подозрительно оправдывается.", "Не голосуйте за меня, я доктор! (наверное)",
           "{x}, а где ты был ночью?", "Опять никого не нашли, мафия радуется.", "Голосую за {x}, интуиция."]
GROUP = "-100777"
KIND_RU = {"kill": "убить", "doncheck": "проверить на комиссара", "heal": "лечить", "check": "проверить",
           "visit": "обслужить 💋", "maniac": "зарезать", "watch": "подглядеть за", "dance": "станцевать для",
           "booze": "подпоить"}


class Recorder:
    """Подмена Telegram: копит всё, что бот отправил, вместе с картинками."""

    def __init__(self, sim):
        self.sim, self.next_id = sim, 1000

    def __call__(self, method, files=None, **p):
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            img = files["photo"][1] if files else None
            buttons = []
            mk = p.get("reply_markup") or {}
            for row in mk.get("inline_keyboard", []):
                buttons.append([b["text"] for b in row])
            if mk.get("keyboard"):
                buttons.append(["⌨️ " + mk["keyboard"][0][0]["text"]])
            self.sim.log(str(p["chat_id"]), p.get("text") or p.get("caption") or "", img, buttons)
            return {"message_id": self.next_id, "chat": {"id": p["chat_id"]}}
        if method == "getChatMember":
            return {"status": "creator" if str(p["user_id"]) == "1" else "member"}
        if method == "getMe":
            return {"username": "krokodil_party_18_bot", "has_main_web_app": False}
        return True


class Sim:
    def __init__(self, n, seed):
        self.n, self.rng = n, random.Random(seed)
        self.feed = []          # (чат, текст, png, кнопки, номер фазы)
        self.god = []           # строки «взгляда ведущего»
        self.phase = "Набор"
        self.knowledge = {}     # кто что знает: комиссар — найденную мафию

    def log(self, chat, text, img, buttons):
        self.feed.append({"chat": chat, "text": text, "img": img, "buttons": buttons, "phase": self.phase})

    # --- ход виртуальных игроков ---

    def choose(self, g, u, kind, targets):
        mafia_target = getattr(self, "_mafia_target", None)
        if kind == E.KILL and mafia_target in targets:
            return mafia_target           # семья договаривается в тайном чате
        if kind == E.KILL:
            self._mafia_target = self.rng.choice(targets)
            return self._mafia_target
        if kind == E.HEAL and self.rng.random() < .35 and u in targets:
            return u                      # доктор иногда лечит себя
        return self.rng.choice(targets)

    def vote_for(self, g, u):
        a = [t for t in E.alive(g) if t != u]
        found = self.knowledge.get(u)
        if found in a:
            return found                  # комиссар голосует за найденную мафию
        if E.is_mafia(g, u):
            return self.rng.choice([t for t in a if not E.is_mafia(g, t)] or a)
        return self.rng.choice(a + [E.SKIP] * 1)

    def say(self, uid, text):
        B.handle({"message": {"message_id": 1, "chat": {"id": int(uid), "type": "private"},
                              "from": {"id": int(uid), "first_name": NAMES[int(uid) - 1]}, "text": text}})

    def group(self, uid, text):
        B.handle({"message": {"message_id": 1, "chat": {"id": int(GROUP), "type": "supergroup", "title": "Симуляция"},
                              "from": {"id": int(uid), "first_name": NAMES[int(uid) - 1]}, "text": text}})
        self.log(GROUP, f"<b>{NAMES[int(uid) - 1]}:</b> {html.escape(text)}", None, [])

    def press(self, uid, data, chat):
        B.handle({"callback_query": {"id": "1", "from": {"id": int(uid), "first_name": NAMES[int(uid) - 1]},
                                     "data": data, "message": {"message_id": 1, "chat": {"id": int(chat)}}}})

    def app(self, uid, payload):
        B.handle({"message": {"message_id": 1, "chat": {"id": int(uid), "type": "private"}, "from": {"id": int(uid)},
                              "web_app_data": {"data": json.dumps(payload)}}})

    def run(self):
        tmp = tempfile.TemporaryDirectory()
        B.DATA = Path(tmp.name)
        B.LOG_PATH = B.DATA / "sim.log"
        B.db_init(B.DATA / "sim.db")
        B.GAMES.clear()
        B.BOT = "krokodil_party_18_bot"
        B.api = B.api_upload = Recorder(self)
        clock = [1_800_000_000.0]
        B.now = lambda: clock[0]
        B._app_check[:] = [0.0, False]
        random.seed(self.rng.random())  # тексты бота тоже воспроизводимы

        self.group(1, "/mafia")
        for u in range(1, self.n + 1):
            self.say(u, f"/start join_{GROUP}")
        self.press(1, f"begin:{GROUP}", GROUP)
        g = B.GAMES[GROUP]
        self.god.append("<h3>Роли</h3><ul>" + "".join(
            f"<li><b>{html.escape(p['name'])}</b> — {E.ROLE_NAMES[p['role']]}"
            + (f" (муж: {html.escape(g['players'][g['husband']]['name'])})" if p["role"] == E.WIFE else "") + "</li>"
            for p in g["players"].values()) + "</ul>")

        for _ in range(80):
            g = B.GAMES.get(GROUP)
            if not g:
                break
            if g["status"] == "night":
                self.phase = f"Ночь {g['night_no']}"
                self._mafia_target = None
                lines, n0 = [], g["night_no"]
                tasks = E.night_tasks(g)
                by_player = {}
                for u, kind, targets in tasks:
                    by_player.setdefault(u, []).append((kind, targets))
                for u, items in by_player.items():
                    if self.rng.random() < .08:
                        lines.append(f"{html.escape(g['players'][u]['name'])} проспал(а) ход")
                        continue
                    acts = {kind: self.choose(g, u, kind, targets) for kind, targets in items}
                    for kind, t in acts.items():
                        lines.append(f"{html.escape(g['players'][u]['name'])} ({E.ROLE_NAMES[g['players'][u]['role']]}) → "
                                     f"{KIND_RU[kind]} <b>{html.escape(g['players'][t]['name'])}</b>")
                        if kind == E.CHECK and E.is_mafia(g, t) and g["players"][u]["role"] == E.SHERIFF:
                            self.knowledge[u] = t
                    if self.rng.random() < .6:   # большинство ходит через приложение «Моя роль»
                        self.app(u, {"a": "n", "c": GROUP, "n": n0, "acts": acts})
                    else:
                        for kind, t in acts.items():
                            self.press(u, f"n:{GROUP}:{kind}:{t}", u)
                    if GROUP not in B.GAMES or B.GAMES[GROUP]["status"] != "night":
                        break
                self.god.append(f"<h3>Ночь {n0}</h3><ul>" + "".join(f"<li>{x}</li>" for x in lines) + "</ul>")
                if GROUP in B.GAMES and B.GAMES[GROUP]["status"] == "night":
                    clock[0] += B.NIGHT_SEC + 1
                    B.tick()
            elif g["status"] == "day":
                self.phase = f"День {g['day_no']}"
                a = E.alive(g)
                for u in self.rng.sample(a, min(3, len(a))):
                    other = self.rng.choice([t for t in a if t != u])
                    self.group(u, self.rng.choice(CHATTER).format(x=g["players"][other]["name"]))
                for u in E.voters(g):
                    self.press(u, f"tv:{GROUP}", GROUP)
                    if B.GAMES.get(GROUP, {}).get("status") != "day":
                        break
                if B.GAMES.get(GROUP, {}).get("status") == "day":
                    clock[0] += B.DAY_SEC + 1
                    B.tick()
            elif g["status"] == "vote":
                lines = []
                for u in list(E.voters(g)):
                    t = self.vote_for(g, u)
                    lines.append(f"{html.escape(g['players'][u]['name'])} → "
                                 + ("пропустить" if t == E.SKIP else f"<b>{html.escape(g['players'][t]['name'])}</b>"))
                    if self.rng.random() < .7:
                        self.app(u, {"a": "v", "c": GROUP, "d": g["day_no"], "t": t})
                    else:
                        self.press(u, f"v:{GROUP}:{t}", GROUP)
                    if B.GAMES.get(GROUP, {}).get("status") != "vote":
                        break
                silenced = g["memory"].get("silenced")
                self.god.append(f"<h3>Тайное голосование, день {g['day_no']}</h3><ul>" + "".join(f"<li>{x}</li>" for x in lines)
                                + (f"<li>💃 {html.escape(g['players'][silenced]['name'])} не голосует</li>" if silenced else "")
                                + "</ul>")
                if B.GAMES.get(GROUP, {}).get("status") == "vote":
                    clock[0] += B.VOTE_SEC + 1
                    B.tick()
        B.db.close()
        tmp.cleanup()
        return self


# ---------- отчёт ----------

def tg_html(text):
    """Telegram-HTML как есть (b, i), переносы — <br>."""
    return text.replace("\n", "<br>")


def render(sim, path):
    names = {str(i + 1): n for i, n in enumerate(NAMES)}
    feed_group = [m for m in sim.feed if m["chat"] == GROUP]
    dms = {}
    for m in sim.feed:
        if m["chat"] != GROUP:
            dms.setdefault(m["chat"], []).append(m)

    def bubble(m):
        img = f'<img src="data:image/png;base64,{base64.b64encode(m["img"]).decode()}" alt="">' if m["img"] else ""
        btns = "".join(f'<div class="btns">{"".join(f"<span>{html.escape(b)}</span>" for b in row)}</div>'
                       for row in m["buttons"])
        return f'<div class="msg">{img}<div class="txt">{tg_html(m["text"])}</div>{btns}</div>'

    group_html, last = [], None
    for m in feed_group:
        if m["phase"] != last:
            group_html.append(f'<div class="phase">{m["phase"]}</div>')
            last = m["phase"]
        group_html.append(bubble(m))
    dm_html = "".join(
        f'<details><summary>{html.escape(names.get(u, u))} — {len(ms)} сообщ.</summary>{"".join(bubble(m) for m in ms)}</details>'
        for u, ms in sorted(dms.items(), key=lambda x: int(x[0])))
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Симуляция Мафии — {sim.n} игроков</title>
<style>
:root {{ color-scheme: dark; --bg:#140f1f; --card:#1d1530; --raise:#2a1f44; --line:#3a2d5c; --fg:#f3eefc; --muted:#a99cc6; --pink:#ff4fa3; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 "Segoe UI", system-ui, sans-serif; }}
header {{ padding:20px 24px; border-bottom:1px solid var(--line); }}
header h1 {{ margin:0 0 4px; font-size:22px; }} header p {{ margin:0; color:var(--muted); }}
.cols {{ display:grid; grid-template-columns: minmax(0,1.3fr) minmax(0,1fr); gap:24px; padding:20px 24px; }}
@media (max-width: 900px) {{ .cols {{ grid-template-columns: 1fr; }} }}
h2 {{ font-size:13px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin:0 0 12px; }}
.msg {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:10px 12px; margin-bottom:10px; max-width:620px; }}
.msg img {{ width:100%; border-radius:10px; display:block; margin-bottom:8px; }}
.btns {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }}
.btns span {{ background:var(--raise); border-radius:8px; padding:4px 10px; font-size:13px; color:var(--muted); }}
.phase {{ position:sticky; top:0; background:var(--bg); padding:8px 0; color:var(--pink); font-weight:700; z-index:1; }}
.god {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:4px 16px 12px; margin-bottom:16px; }}
.god h3 {{ font-size:15px; margin:12px 0 4px; color:var(--pink); }} .god ul {{ margin:0; padding-left:20px; }}
details {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:8px 12px; margin-bottom:8px; }}
summary {{ cursor:pointer; font-weight:600; }} details .msg {{ margin-top:8px; }}
</style></head><body>
<header><h1>Симуляция Мафии 18+ — {sim.n} игроков</h1>
<p>Виртуальные игроки прошли партию через настоящий код бота. Слева — что видит группа, справа — взгляд ведущего и лички.</p></header>
<div class="cols"><section><h2>Группа</h2>{"".join(group_html)}</section>
<section><h2>Взгляд ведущего</h2><div class="god">{"".join(sim.god)}</div><h2>Лички игроков</h2>{dm_html}</section></div>
</body></html>"""
    path.write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Симуляция партии Мафии с отчётом в HTML")
    ap.add_argument("--players", type=int, default=12, help=f"{E.MIN_PLAYERS}–{E.MAX_PLAYERS}, по умолчанию 12")
    ap.add_argument("--seed", type=int, default=None, help="номер партии — тот же номер даёт ту же партию")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "simulation.html"))
    ap.add_argument("--no-open", action="store_true", help="не открывать отчёт в браузере")
    args = ap.parse_args()
    seed = args.seed if args.seed is not None else random.randrange(10_000)
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    sim = Sim(args.players, seed).run()
    render(sim, out)
    print(f"партия №{seed}, игроков {args.players}: сообщений в группе {sum(m['chat'] == GROUP for m in sim.feed)}, "
          f"картинок {sum(bool(m['img']) for m in sim.feed)} → {out}")
    if not args.no_open and hasattr(os, "startfile"):
        os.startfile(out)


if __name__ == "__main__":
    main()
