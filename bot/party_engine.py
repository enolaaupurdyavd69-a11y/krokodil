"""Крокодил и Табу в группе: словари и сравнение слов (без Telegram).

Сравнение терпимо к регистру, ё/е, знакам препинания и окончаниям:
«Шахматы» засчитывается на «шахматы!», «в шахматах», «Шахматист».
"""
import json
import random
import re
from pathlib import Path

WORDS_DIR = Path(__file__).resolve().parent.parent / "words"

# Служебные слова не обязательны в догадке и не считаются нарушением
STOP = {"и", "в", "во", "на", "с", "со", "не", "у", "за", "по", "к", "ко", "о", "об", "от", "до", "из",
        "а", "но", "что", "как", "это", "то", "же", "ли", "бы", "для", "при", "под", "над"}


def load_decks(words_dir=WORDS_DIR):
    """{'croc': [{'t', 'c'}], 'tabu': [{'t', 'x', 'c'}]} из тех же файлов, что и сайт."""
    idx = json.loads((words_dir / "index.json").read_text(encoding="utf-8"))
    decks = {"croc": [], "tabu": []}
    for c in idx:
        for w in json.loads((words_dir / f"{c['id']}.json").read_text(encoding="utf-8")):
            decks["croc"].append({"t": w["t"], "c": c["name"]})
        for w in json.loads((words_dir / "tabu" / f"{c['id']}.json").read_text(encoding="utf-8")):
            decks["tabu"].append({"t": w["t"], "x": w["x"], "c": c["name"]})
    return decks


def norm(text):
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def significant(tokens, min_len=2):
    return [t for t in tokens if t not in STOP and len(t) >= min_len]


def stem(word):
    """Грубая основа: ~60% слова, минимум 4 буквы. «проснуться» → «просну», «шахматы» → «шахм»."""
    return word if len(word) <= 4 else word[:max(4, int(len(word) * 0.6 + 0.5))]


def same_root(word, token):
    if len(word) <= 3:
        return token == word
    return token.startswith(stem(word))


def close_form(word, token):
    """Для догадок строже, чем same_root: общее начало — не меньше 60% обоих слов.

    «проснулся» = «проснуться», «шахматах» = «шахматы», но «каратист» ≠ «караоке», «пинг» ≠ «пингвин».
    """
    if len(word) <= 3:
        return token == word
    p = 0
    for a, b in zip(word, token):
        if a != b:
            break
        p += 1
    return p >= 4 and p >= .6 * len(word) and p >= .6 * len(token)


def guessed(answer, message):
    """Догадка верна, если в ней есть все значимые слова ответа (с любыми окончаниями).

    Слишком длинные сообщения не засчитываются — чтобы нельзя было угадать перечислением.
    """
    need = significant(norm(answer)) or norm(answer)
    got = norm(message)
    if not need or len(got) > len(need) + 3:
        return False
    return all(any(close_form(n, g) for g in got) for n in need)


def violations(card, mode, message):
    """Запретные слова в сообщении ведущего: само слово, однокоренные, в Табу — ещё 5 с карточки."""
    banned = significant(norm(card["t"]), 3)
    if mode == "tabu":
        for x in card["x"]:
            banned += significant(norm(x), 3)
    got = norm(message)
    return [b for b in dict.fromkeys(banned) if any(same_root(b, g) for g in got)]


def draw(deck, used, rng=random):
    """Случайная карточка, которой ещё не было в этом чате; когда кончились — по новой."""
    left = [c for c in deck if c["t"] not in used]
    if not left:
        used.clear()
        left = deck
    card = rng.choice(left)
    used.append(card["t"])
    return card
