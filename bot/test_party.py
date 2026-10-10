"""Тесты Крокодила и Табу в группе: сравнение слов и игра через поддельный Telegram.

Запуск из папки bot:  python -X utf8 -m unittest -v test_party
"""
import tempfile
import unittest
from pathlib import Path

import mafia_bot as B
import party_engine as P
from test_mafia import FakeTelegram, GROUP

POHMELYE = {"t": "Похмелье", "x": ["утро", "голова", "рассол", "пить", "алкоголь"], "c": "Вечеринка"}


class Matching(unittest.TestCase):
    def test_guess_forms(self):
        self.assertTrue(P.guessed("Шахматы", "Шахматы!"))
        self.assertTrue(P.guessed("Шахматы", "в шахматах"))
        self.assertTrue(P.guessed("Ёлки", "елки"))
        self.assertTrue(P.guessed("Проснуться не у себя дома", "проснулся у себя не дома"))
        self.assertFalse(P.guessed("Шахматы", "шашки"))
        self.assertFalse(P.guessed("Проснуться не у себя дома", "дома"))

    def test_no_guess_by_listing(self):
        self.assertFalse(P.guessed("Зонт", "чайник зонт утюг кактус лифт"))

    def test_violations(self):
        self.assertEqual(P.violations(POHMELYE, "croc", "когда наутро плохо после праздника"), [])
        self.assertEqual(P.violations(POHMELYE, "croc", "похмельный синдром"), ["похмелье"])
        self.assertEqual(P.violations(POHMELYE, "tabu", "утром болит голова"), ["утро", "голова"])
        self.assertEqual(P.violations(POHMELYE, "croc", "утром болит голова"), [], "в Крокодиле запретных нет")

    def test_decks_and_draw(self):
        decks = P.load_decks()
        self.assertEqual(len(decks["croc"]), 300)
        self.assertEqual(len(decks["tabu"]), 180)
        used = []
        seen = {P.draw(decks["tabu"], used)["t"] for _ in range(180)}
        self.assertEqual(len(seen), 180, "без повторов, пока колода не кончилась")


class GroupFlow(unittest.TestCase):
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
        self.n = 0

    def tearDown(self):
        B.db.close()
        self.tmp.cleanup()

    def say(self, uid, text):
        self.n += 1
        B.handle({"message": {"message_id": self.n, "chat": {"id": int(GROUP), "type": "supergroup", "title": "Пятница"},
                              "from": {"id": int(uid), "first_name": f"Игрок{uid}"}, "text": text}})

    def press(self, uid, data):
        self.n += 1
        B.handle({"callback_query": {"id": str(self.n), "from": {"id": int(uid), "first_name": f"Игрок{uid}"},
                                     "data": data, "message": {"message_id": 1, "chat": {"id": int(GROUP)}}}})

    def last_answer(self):
        return [p for m, p in self.tg.sent if m == "answerCallbackQuery"][-1]

    def group_text(self):
        return self.tg.to(GROUP)[-1]["text"]

    def score(self, uid):
        return B.GAMES[GROUP]["scores"][str(uid)]["score"]

    def test_krokodil_round(self):
        self.say(1, "/krokodil")
        g = B.GAMES[GROUP]
        self.assertEqual(g["status"], "idle")
        self.press(2, f"pl:{GROUP}")
        self.assertEqual(g["leader"], "2")
        g["card"] = {"t": "Шахматы", "c": "Обычные"}
        self.press(3, f"pw:{GROUP}")
        self.assertTrue(self.last_answer()["show_alert"])
        self.assertNotIn("Шахматы", self.last_answer()["text"], "слово видит только ведущий")
        self.press(2, f"pw:{GROUP}")
        self.assertIn("Шахматы", self.last_answer()["text"])
        self.say(2, "игра на доске, есть король и ферзь")
        self.assertEqual(self.score(2), 0, "ведущий объясняет честно — без штрафа")
        self.say(3, "шашки?")
        self.assertEqual(g["leader"], "2")
        self.say(3, "шахматы!")
        self.assertEqual((self.score(3), self.score(2)), (1, 1))
        self.assertEqual(g["leader"], "3", "угадавший ведёт следующим")
        word = g["card"]["t"]
        self.say(3, f"это {word}")
        self.assertEqual(self.score(3), 0, "назвал слово — минус очко")
        self.assertIn("запретное", self.group_text())
        self.clock[0] += B.PARTY_TURN_SEC + 1
        B.tick()
        self.assertEqual(g["status"], "idle")
        self.assertIn("Время вышло", self.group_text())
        self.say(4, "/score")
        self.assertIn("Игрок2 — 1", self.group_text())
        self.say(5, "/stop")
        self.assertIn("начал игру", self.group_text(), "чужой не может закончить")
        self.say(1, "/stop")
        self.assertNotIn(GROUP, B.GAMES)
        self.assertIn("Счёт", self.group_text())

    def test_tabu_forbidden(self):
        self.say(1, "/tabu")
        self.press(1, f"pl:{GROUP}")
        g = B.GAMES[GROUP]
        g["card"] = dict(POHMELYE)
        self.press(1, f"pw:{GROUP}")
        self.assertIn("Нельзя: утро", self.last_answer()["text"])
        self.say(1, "бывает утром после вечеринки")
        self.assertEqual(self.score(1), -1)
        self.assertNotEqual(g["card"]["t"], "Похмелье", "после нарушения слово меняется")

    def test_one_game_per_chat_and_pass(self):
        self.say(1, "/krokodil")
        self.say(1, "/mafia")
        self.assertIn("уже идёт", self.group_text())
        self.say(1, "/tabu")
        self.assertIn("уже идёт", self.group_text())
        self.press(2, f"pl:{GROUP}")
        self.press(3, f"pl:{GROUP}")
        self.assertIn("уже есть", self.last_answer()["text"])
        self.press(2, f"pp:{GROUP}")
        self.assertEqual(B.GAMES[GROUP]["status"], "idle")
        self.assertIn("передаёт ход", self.group_text())

    def test_idle_closes(self):
        self.say(1, "/krokodil")
        self.clock[0] += B.PARTY_IDLE_SEC + 1
        B.tick()
        self.assertNotIn(GROUP, B.GAMES)

    def test_menu_on_add_and_buttons(self):
        self.n += 1
        B.handle({"message": {"message_id": self.n, "chat": {"id": int(GROUP), "type": "supergroup", "title": "Мафия"},
                              "from": {"id": 1, "first_name": "Админ"},
                              "new_chat_members": [{"id": 999, "is_bot": True, "username": "krokodil_party_18_bot"}]}})
        menu = self.tg.to(GROUP)[-1]
        self.assertIn("Во что играем", menu["text"])
        buttons = [b["callback_data"] for row in menu["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["gm:mafia", "gm:croc", "gm:tabu", "gm:rules"])
        pins = [p for m, p in self.tg.sent if m == "pinChatMessage"]
        self.assertEqual(len(pins), 1)
        # повторное /games: прошлое меню открепляется, новое закрепляется
        self.say(2, "/games")
        self.assertEqual([m for m, _ in self.tg.sent if m in ("unpinChatMessage", "pinChatMessage")][-2:],
                         ["unpinChatMessage", "pinChatMessage"])
        self.press(3, "gm:croc")
        self.assertEqual(B.GAMES[GROUP]["mode"], "croc")
        self.press(3, "gm:mafia")
        self.assertIn("уже идёт", self.group_text(), "кнопка не ломает идущую игру")
        self.say(3, "/stop")  # закончить может тот, кто начал игру кнопкой
        self.press(4, "gm:mafia")
        self.assertEqual(B.GAMES[GROUP]["status"], "lobby")
        self.assertEqual(B.GAMES[GROUP]["creator"], "4", "набор открыл нажавший кнопку")

    def test_survives_restart(self):
        self.say(1, "/krokodil")
        self.press(2, f"pl:{GROUP}")
        card = B.GAMES[GROUP]["card"]["t"]
        B.GAMES.clear()
        B.GAMES.update(B.load_games())
        self.say(3, card)
        self.assertEqual(self.score(3), 1)


if __name__ == "__main__":
    unittest.main()
