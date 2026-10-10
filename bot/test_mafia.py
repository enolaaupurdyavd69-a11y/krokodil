"""Тесты Мафии: логика и полные партии с виртуальными игроками через поддельный Telegram.

Запуск из папки bot:  python -X utf8 -m unittest -v test_mafia
"""
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
            mafia = plan[E.MAFIA] + plan[E.DON]
            self.assertLess(mafia, n - mafia, f"мафии не должно быть поровну с городом на старте, n={n}")
            self.assertEqual(plan[E.LOVER], 1 if n >= 7 else 0)
            self.assertEqual(plan[E.MANIAC], 1 if n >= 9 else 0)
            self.assertEqual(plan[E.DON], 1 if n >= 7 else 0)
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

    def __call__(self, method, **p):
        self.sent.append((method, p))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": p["chat_id"]}}
        if method == "getChatMember":
            return {"status": "administrator" if str(p["user_id"]) in self.admins else "member"}
        return True

    def to(self, chat):
        return [p for m, p in self.sent if m == "sendMessage" and str(p["chat_id"]) == str(chat)]

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
        B.api = self.tg
        self.clock = [1_000_000.0]
        B.now = lambda: self.clock[0]
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
