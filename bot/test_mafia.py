"""Тесты Мафии: логика и полные партии с виртуальными игроками через поддельный Telegram.

Запуск из папки bot:  python -X utf8 -m unittest -v test_mafia
"""
import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import mafia_engine as E
import mafia_bot as B


def make(roles):
    """Партия с заданными ролями: игроки '1', '2', … по порядку."""
    st = E.new_game({str(i): f"P{i}" for i in range(1, len(roles) + 1)})
    for i, r in enumerate(roles, 1):
        st["players"][str(i)]["role"] = r
    return st


class Engine(unittest.TestCase):
    def test_role_plan(self):
        for n in range(E.MIN_PLAYERS, E.MAX_PLAYERS + 1):
            plan = Counter(E.role_plan(n))
            self.assertEqual(sum(plan.values()), n)
            mafia = plan[E.MAFIA] + plan[E.DON] + plan[E.BARTENDER]
            self.assertLess(mafia, n - mafia, f"мафии не должно быть поровну с городом на старте, n={n}")
            for role, need in ((E.LOVER, 7), (E.VOYEUR, 8), (E.MANIAC, 9), (E.STRIPPER, 10),
                               (E.WIFE, 11), (E.BARTENDER, 12), (E.DON, 7)):
                self.assertEqual(plan[role], 1 if n >= need else 0, f"{role} при n={n}")
        with self.assertRaises(ValueError):
            E.role_plan(4)

    def test_kill_and_heal(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL])
        E.start_night(st)
        self.assertTrue(E.record_action(st, "1", E.KILL, "4"))
        self.assertFalse(E.record_action(st, "1", E.KILL, "1"), "мафия не может убить своего")
        E.record_action(st, "2", E.HEAL, "4")
        E.record_action(st, "3", E.CHECK, "1")
        self.assertTrue(E.night_done(st))
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], [])
        self.assertEqual(res["saved"], ["4"])
        self.assertIn(("3", "1", E.CHECK, True), res["checks"])
        # того же доктор второй раз подряд лечить не может
        E.start_night(st)
        self.assertFalse(E.record_action(st, "2", E.HEAL, "4"))

    def test_doctor_self_heal_once(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL])
        E.start_night(st)
        self.assertTrue(E.record_action(st, "2", E.HEAL, "2"))
        E.resolve_night(st)
        E.start_night(st)
        E.record_action(st, "2", E.HEAL, "3")
        E.resolve_night(st)
        E.start_night(st)
        self.assertFalse(E.record_action(st, "2", E.HEAL, "2"), "себя — только один раз за игру")

    def test_lover_blocks(self):
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "1", E.KILL, "6")
        E.record_action(st, "2", E.KILL, "7")
        E.record_action(st, "5", E.VISIT, "1")   # дон отвлечён: его голос не считается
        E.record_action(st, "3", E.HEAL, "6")
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], ["7"])
        self.assertEqual(res["blocked"], "1")

    def test_don_breaks_tie(self):
        st = make([E.MAFIA, E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL, E.CIVIL, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "1", E.KILL, "6")
        E.record_action(st, "2", E.KILL, "7")
        E.record_action(st, "3", E.KILL, "8")
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], ["7"])

    def test_maniac_and_mafia_both_kill(self):
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.MANIAC, E.CIVIL, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "1", E.KILL, "7")
        E.record_action(st, "2", E.KILL, "7")
        E.record_action(st, "6", E.MANIAC_KILL, "8")
        E.record_action(st, "3", E.HEAL, "8")
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], ["7"])
        self.assertEqual(res["saved"], ["8"])

    def test_stripper_silences_vote(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.STRIPPER, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "4", E.DANCE, "5")
        res = E.resolve_night(st)
        self.assertEqual(res["silenced"], "5")
        E.start_vote(st)
        self.assertFalse(E.record_vote(st, "5", "1"), "после танца не голосует")
        for v in "1234" + "6":
            E.record_vote(st, v, E.SKIP)
        self.assertTrue(E.vote_done(st), "голосование не ждёт молчащего")
        E.start_night(st)
        self.assertNotIn("5", [u for u in E.alive(st) if u not in E.voters(st)], "запрет только на один день")

    def test_voyeur_sees_visitors(self):
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.VOYEUR, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "1", E.KILL, "7")
        E.record_action(st, "3", E.HEAL, "7")
        E.record_action(st, "5", E.VISIT, "8")
        E.record_action(st, "6", E.WATCH, "7")
        res = E.resolve_night(st)
        voyeur, target, seen = res["watch"]
        self.assertEqual((voyeur, target), ("6", "7"))
        self.assertEqual(set(seen), {"1", "3"}, "видно мафиози и доктора, Путана была у другого")

    def test_jealous_wife_kills_lover(self):
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.WIFE, E.CIVIL, E.CIVIL])
        st["husband"] = "7"
        E.start_night(st)
        E.record_action(st, "5", E.VISIT, "7")
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], ["5"])
        self.assertEqual(res["jealous"], "5")
        self.assertFalse(st["players"]["5"]["alive"])

    def test_wife_wins_with_husband(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.WIFE, E.CIVIL])
        st["husband"] = "5"
        self.assertTrue(E.wife_won(st))
        st["players"]["5"]["alive"] = False
        self.assertFalse(E.wife_won(st))

    def test_bartender_fools_sheriff_and_doctor(self):
        st = make([E.DON, E.BARTENDER, E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL, E.CIVIL, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "2", E.BOOZE, "5")       # пьяный комиссар
        E.record_action(st, "5", E.CHECK, "1")        # проверяет дона
        res = E.resolve_night(st)
        self.assertIn(("5", "1", E.CHECK, False), res["checks"], "пьяному дон кажется мирным")
        E.start_night(st)
        E.record_action(st, "2", E.BOOZE, "4")       # пьяный доктор
        E.record_action(st, "1", E.KILL, "6")
        E.record_action(st, "4", E.HEAL, "6")
        res = E.resolve_night(st)
        self.assertEqual(res["dead"], ["6"], "пьяный доктор не вылечил")

    def test_vote(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL])
        E.start_vote(st)
        self.assertFalse(E.record_vote(st, "1", "1"), "за себя нельзя")
        for v in "2345":
            E.record_vote(st, v, "1")
        E.record_vote(st, "1", E.SKIP)
        self.assertTrue(E.vote_done(st))
        self.assertEqual(E.resolve_vote(st), "1")
        self.assertEqual(E.winner(st), "town")

    def test_vote_tie_and_skip(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL])
        E.start_vote(st)
        E.record_vote(st, "1", "2"); E.record_vote(st, "2", "1")
        self.assertIsNone(E.resolve_vote(st), "ничья — никого")
        E.start_vote(st)
        E.record_vote(st, "1", "2"); E.record_vote(st, "3", E.SKIP); E.record_vote(st, "4", E.SKIP)
        self.assertIsNone(E.resolve_vote(st), "пропусков больше — никого")

    def test_winner(self):
        st = make([E.MAFIA, E.DOCTOR, E.SHERIFF, E.CIVIL, E.CIVIL])
        self.assertIsNone(E.winner(st))
        for u in "234":
            st["players"][u]["alive"] = False
        self.assertEqual(E.winner(st), "mafia")
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.MANIAC, E.CIVIL, E.CIVIL, E.CIVIL])
        for u in "1234579":
            st["players"][u]["alive"] = False
        self.assertEqual(E.winner(st), "maniac")

    def test_random_games_finish(self):
        rng = random.Random(7)
        for _ in range(500):
            n = rng.randint(E.MIN_PLAYERS, E.MAX_PLAYERS)
            st = E.new_game({str(i): f"P{i}" for i in range(n)}, rng)
            for _round in range(40):
                E.start_night(st)
                for u, k, targets in E.night_tasks(st):
                    if rng.random() < .9:
                        self.assertTrue(E.record_action(st, u, k, rng.choice(targets)))
                E.resolve_night(st)
                if E.winner(st):
                    break
                E.start_vote(st)
                a = E.alive(st)
                for u in a:
                    E.record_vote(st, u, rng.choice([t for t in a if t != u] + [E.SKIP]))
                E.resolve_vote(st)
                if E.winner(st):
                    break
            self.assertIsNotNone(E.winner(st), "партия должна заканчиваться")


class FakeTelegram:
    """Подмена B.api: запоминает отправленное и выдаёт номера сообщений."""

    def __init__(self):
        self.sent, self.next_id, self.admins = [], 100, set()

    has_app = False

    def __call__(self, method, files=None, **p):
        if method == "sendPhoto":
            assert files and files["photo"][1][1:4] == b"PNG", "картинка должна быть PNG"
            p["text"] = p.get("caption", "")
            self.photos = getattr(self, "photos", 0) + 1
        self.sent.append((method, p))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": p["chat_id"]}}
        if method == "getChatMember":
            return {"status": "administrator" if str(p["user_id"]) in self.admins else "member"}
        if method == "getMe":
            return {"username": "krokodil_party_18_bot", "has_main_web_app": self.has_app}
        return True

    def to(self, chat):
        return [p for m, p in self.sent if m in ("sendMessage", "sendPhoto") and str(p["chat_id"]) == str(chat)]

    def last_buttons(self, chat):
        msgs = [p for p in self.to(chat) if "reply_markup" in p]
        return [b for row in msgs[-1]["reply_markup"]["inline_keyboard"] for b in row]


GROUP = "-1001"


class BotFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        B.DATA = Path(self.tmp.name)
        B.LOG_PATH = B.DATA / "bot.log"
        B.db_init(B.DATA / "test.db")
        B.GAMES.clear()
        B.BOT = "krokodil_party_18_bot"
        self.tg = FakeTelegram()
        B.api = B.api_upload = self.tg
        self.clock = [1_000_000.0]
        B.now = lambda: self.clock[0]
        B._app_check[:] = [0.0, False]
        self.cq_id = 0

    def tearDown(self):
        B.db.close()
        self.tmp.cleanup()

    def msg(self, chat_id, chat_type, uid, text, title=None):
        chat = {"id": int(chat_id), "type": chat_type}
        if title:
            chat["title"] = title
        B.handle({"message": {"chat": chat, "from": {"id": int(uid), "first_name": f"Игрок{uid}"}, "text": text}})

    def press(self, uid, data, chat=None):
        self.cq_id += 1
        B.handle({"callback_query": {"id": str(self.cq_id), "from": {"id": int(uid)}, "data": data,
                                     "message": {"message_id": 1, "chat": {"id": int(chat or uid)}}}})

    def answers(self):
        return [p.get("text", "") for m, p in self.tg.sent if m == "answerCallbackQuery"]

    def lobby(self, n):
        self.msg(GROUP, "supergroup", 1, "/mafia@krokodil_party_18_bot", "Пятница")
        for u in range(1, n + 1):
            self.msg(u, "private", u, f"/start join_{GROUP}")
        return B.GAMES[GROUP]

    def test_lobby_and_permissions(self):
        g = self.lobby(4)
        self.assertEqual(len(g["joined"]), 4)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertIn("минимум 5", self.answers()[-1])
        self.msg(5, "private", 5, f"/start join_{GROUP}")
        self.press(3, f"begin:{GROUP}", GROUP)
        self.assertIn("открыл набор", self.answers()[-1], "чужой не может начать")
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertEqual(g["status"], "night")
        for u in range(1, 6):
            self.assertTrue(any("Ваша роль" in p["text"] for p in self.tg.to(u)), f"игрок {u} без роли")
        self.msg(GROUP, "supergroup", 2, "/mafia")
        self.assertIn("уже идёт", self.tg.to(GROUP)[-1]["text"])

    def test_mafia_chat_relay(self):
        g = self.lobby(7)
        self.press(1, f"begin:{GROUP}", GROUP)
        don = next(u for u, p in g["players"].items() if p["role"] == E.DON)
        mafia = next(u for u, p in g["players"].items() if p["role"] == E.MAFIA)
        civil = next(u for u, p in g["players"].items() if not E.is_mafia(g, u))
        self.msg(don, "private", don, "берём того, кто громче всех смеётся")
        self.assertIn("громче всех", self.tg.to(mafia)[-1]["text"])
        before = len(self.tg.to(don))
        self.msg(civil, "private", civil, "я тоже хочу в чат мафии")
        self.assertEqual(len(self.tg.to(don)), before, "мирный не попадает в чат мафии")

    def play(self, n, rng):
        """Полная партия: все кнопки — случайные, часть ходов пропускается по таймеру."""
        g = self.lobby(n)
        self.press(1, f"begin:{GROUP}", GROUP)
        for _ in range(60):
            if GROUP not in B.GAMES:
                return
            g = B.GAMES[GROUP]
            if g["status"] == "night":
                for u, kind, targets in E.night_tasks(g):
                    if rng.random() < .85:
                        self.press(u, f"n:{GROUP}:{kind}:{rng.choice(targets)}")
                    if g["status"] != "night":
                        break
                if g["status"] == "night":  # кто-то не успел — ждём конца ночи
                    self.clock[0] += B.NIGHT_SEC + 1
                    B.tick()
            elif g["status"] == "day":
                for u in E.alive(g):
                    self.press(u, f"tv:{GROUP}", GROUP)
                    if g["status"] != "day":
                        break
            elif g["status"] == "vote":
                a = E.alive(g)
                for u in a:
                    if rng.random() < .9:
                        self.press(u, f"v:{GROUP}:{rng.choice([t for t in a if t != u] + [E.SKIP])}", GROUP)
                    if g["status"] != "vote":
                        break
                if g["status"] == "vote":
                    self.clock[0] += B.VOTE_SEC + 1
                    B.tick()
        self.fail("партия не закончилась за 60 шагов")

    def test_full_games(self):
        rng = random.Random(3)
        for n in (5, 7, 9, 12, 15):
            with self.subTest(players=n):
                self.tg.sent.clear()
                self.play(n, rng)
                final = self.tg.to(GROUP)[-1]["text"]
                self.assertIn("Роли:", final)
                self.assertTrue(any(m == "sendPhoto" and str(p["chat_id"]) == GROUP for m, p in self.tg.sent),
                                "ночь/утро/финал — картинками")
                self.assertEqual(B.load_games(), {}, "законченная партия удалена из базы")

    def test_restart_resumes(self):
        self.lobby(5)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertEqual(B.GAMES[GROUP]["status"], "night")
        # «перезагрузка ПК»: память процесса пропала, база осталась, прошло 10 минут
        B.kv_set("heartbeat", self.clock[0])
        B.GAMES.clear()
        self.clock[0] += 600
        B.GAMES.update(B.load_games())
        B.resume_after_downtime()
        g = B.GAMES[GROUP]
        self.assertEqual(g["status"], "night")
        self.assertGreater(g["deadline"], self.clock[0], "после простоя дали время доиграть фазу")
        self.assertIn("вернулся", self.tg.to(GROUP)[-1]["text"])
        self.clock[0] += B.RESUME_GRACE + 1
        B.tick()
        self.assertIn(B.GAMES.get(GROUP, {}).get("status", "over"), ("day", "over"))

    def keyboard_data(self, uid):
        """Данные, зашитые в кнопку «🎭 Моя роль» последнего сообщения игроку."""
        import base64, json, urllib.parse
        for p in reversed(self.tg.to(uid)):
            kb = p.get("reply_markup", {}).get("keyboard")
            if kb:
                url = kb[0][0]["web_app"]["url"]
                d = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["d"][0]
                return url, json.loads(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)).decode("utf-8"))
        self.fail(f"у игрока {uid} нет кнопки «Моя роль»")

    def test_role_card_and_webapp_move(self):
        g = self.lobby(7)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertGreaterEqual(self.tg.photos, 7 + 1, "карточки ролей в личку и баннер ночи в группу")
        don = next(u for u, p in g["players"].items() if p["role"] == E.DON)
        url, data = self.keyboard_data(don)
        self.assertTrue(url.startswith(B.MAFIA_URL))
        self.assertEqual(data["r"], E.DON)
        self.assertEqual(set(data["t"]), {E.KILL, E.DON_CHECK}, "дону — убийство и проверка")
        self.assertEqual(len(data["m"]), 1, "дон видит свою семью")
        victim = data["t"][E.KILL][0]
        # ход из мини-приложения приходит боту сообщением web_app_data
        B.handle({"message": {"chat": {"id": int(don), "type": "private"}, "from": {"id": int(don)},
                              "web_app_data": {"data": json.dumps({"a": "n", "c": GROUP, "n": 1,
                                               "acts": {E.KILL: victim, E.DON_CHECK: data["t"][E.DON_CHECK][1]}})}}})
        self.assertEqual(g["night"]["acts"][f"{don}:{E.KILL}"], victim)
        self.assertIn("Ход из приложения", self.tg.to(don)[-1]["text"])
        # повтор того же хода — отказ, а не двойной голос
        B.handle({"message": {"chat": {"id": int(don), "type": "private"}, "from": {"id": int(don)},
                              "web_app_data": {"data": json.dumps({"a": "n", "c": GROUP, "n": 1, "acts": {E.KILL: victim}})}}})
        self.assertIn("уже сделан", self.tg.to(don)[-1]["text"])
        # ход за прошлую ночь не принимается
        B.handle({"message": {"chat": {"id": int(don), "type": "private"}, "from": {"id": int(don)},
                              "web_app_data": {"data": json.dumps({"a": "n", "c": GROUP, "n": 0, "acts": {}})}}})
        self.assertIn("закончилась", self.tg.to(don)[-1]["text"])

    def test_secret_vote_via_app(self):
        g = self.lobby(5)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.clock[0] += B.NIGHT_SEC + 1
        B.tick()                                # ночь без ходов
        g = B.GAMES[GROUP]
        for u in E.alive(g):
            self.press(u, f"tv:{GROUP}", GROUP)
            if g["status"] != "day":
                break
        self.assertEqual(g["status"], "vote")
        voter, target = E.alive(g)[0], E.alive(g)[1]
        url, data = self.keyboard_data(voter)
        self.assertIn(target, data["v"])
        B.handle({"message": {"chat": {"id": int(voter), "type": "private"}, "from": {"id": int(voter)},
                              "web_app_data": {"data": json.dumps({"a": "v", "c": GROUP, "d": g["day_no"], "t": target})}}})
        self.assertEqual(g["votes"][voter], target)
        group_texts = " ".join(p["text"] for m, p in self.tg.sent
                               if m in ("sendMessage", "editMessageText") and str(p["chat_id"]) == GROUP)
        self.assertIn("Проголосовали <b>1 из", group_texts)
        self.assertNotIn(g["players"][voter]["name"] + "</b> —", group_texts, "кто за кого — не видно")

    def test_krokodil_opens_app(self):
        self.msg(GROUP, "supergroup", 1, "/krokodil", "Пятница")
        btn = self.tg.to(GROUP)[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(btn["url"], B.APP_URL + "?mode=croc")
        self.tg.has_app = True
        B._app_check[:] = [0.0, False]
        self.msg(GROUP, "supergroup", 1, "/tabu", "Пятница")
        btn = self.tg.to(GROUP)[-1]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(btn["url"], "https://t.me/krokodil_party_18_bot?startapp=tabu",
                         "с мини-приложением в BotFather — открывается внутри Telegram")
        self.assertNotIn(GROUP, B.GAMES, "в группе ничего не запускается — игра на одном телефоне")

    def test_bots_fill_the_game(self):
        """1 человек + 6 ботов: партия доходит до конца, ботам ничего не отправляется."""
        self.msg(GROUP, "supergroup", 1, "/mafia", "Пятница")
        self.msg(1, "private", 1, f"/start join_{GROUP}")
        self.press(2, f"addbot:{GROUP}", GROUP)
        self.assertIn("открыл набор", self.answers()[-1], "чужой не добавляет ботов")
        for _ in range(7):
            self.press(1, f"addbot:{GROUP}", GROUP)
        self.press(1, f"delbot:{GROUP}", GROUP)
        g = B.GAMES[GROUP]
        self.assertEqual(sum(B.is_bot(u) for u in g["joined"]), 6)
        self.assertIn("(бот)", "".join(g["joined"].values()))
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertEqual(g["status"], "night")
        for _ in range(80):
            g = B.GAMES.get(GROUP)
            if not g:
                break
            if g["status"] == "night" and g["players"]["1"]["alive"]:
                for u, kind, targets in E.night_tasks(g):
                    if u == "1" and f"1:{kind}" not in g["night"]["acts"]:
                        self.press(1, f"n:{GROUP}:{kind}:{targets[0]}")
            elif g["status"] == "day" and "1" in E.voters(g):
                self.press(1, f"tv:{GROUP}", GROUP)
            elif g["status"] == "vote" and "1" in E.voters(g) and "1" not in g["votes"]:
                self.press(1, f"v:{GROUP}:{E.SKIP}", GROUP)
            if GROUP in B.GAMES and B.GAMES[GROUP]["status"] == g["status"]:
                self.clock[0] += 5  # время идёт — боты делают ходы по своим таймерам
                B.tick()
        self.assertNotIn(GROUP, B.GAMES, "партия с ботами должна закончиться")
        self.assertIn("Роли:", self.tg.to(GROUP)[-1]["text"])
        to_bots = [p["chat_id"] for m, p in self.tg.sent if str(p.get("chat_id", "")).startswith("b")]
        self.assertEqual(to_bots, [], "ботам ничего не отправляем")
        self.assertTrue(any("🤖" in p["text"] for p in self.tg.to(GROUP)), "боты пишут реплики днём")

    def test_only_bots_cannot_start(self):
        self.msg(GROUP, "supergroup", 1, "/mafia", "Пятница")
        for _ in range(5):
            self.press(1, f"addbot:{GROUP}", GROUP)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.assertIn("живой игрок", self.answers()[-1])
        self.assertEqual(B.GAMES[GROUP]["status"], "lobby")

    def test_bot_mafia_follows_family(self):
        st = make([E.DON, E.MAFIA, E.DOCTOR, E.SHERIFF, E.LOVER, E.CIVIL, E.CIVIL])
        E.start_night(st)
        E.record_action(st, "1", E.KILL, "6")          # живой дон выбрал жертву
        targets = [t for u, k, ts in E.night_tasks(st) if u == "2" and k == E.KILL for t in ts]
        for _ in range(20):
            self.assertEqual(B.bot_target(st, "2", E.KILL, targets), "6", "бот-мафиози поддерживает семью")

    def test_dead_cannot_vote(self):
        g = self.lobby(5)
        self.press(1, f"begin:{GROUP}", GROUP)
        self.clock[0] += B.NIGHT_SEC + 1
        B.tick()  # ночь без ходов — все живы
        g = B.GAMES[GROUP]
        dead = E.alive(g)[0]
        g["players"][dead]["alive"] = False
        self.press(dead, f"tv:{GROUP}", GROUP)
        self.assertIn("Мёртвые", self.answers()[-1])


if __name__ == "__main__":
    unittest.main()
