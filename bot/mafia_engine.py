"""Логика Мафии без Telegram: роли, ночь, голосование, победа.

Состояние партии — обычный dict (бот хранит его в SQLite как JSON), id игроков — строки.
players: {uid: {"name": str, "role": str, "alive": bool}}
"""
import random
from collections import Counter

MAFIA, DON, BARTENDER = "mafia", "don", "bartender"
DOCTOR, SHERIFF, LOVER, VOYEUR, STRIPPER, CIVIL = "doctor", "sheriff", "lover", "voyeur", "stripper", "civil"
MANIAC, WIFE = "maniac", "wife"
MAFIA_TEAM = {MAFIA, DON, BARTENDER}
MIN_PLAYERS, MAX_PLAYERS = 5, 15

ROLE_NAMES = {
    MAFIA: "Мафия", DON: "Дон мафии", BARTENDER: "Бармен", DOCTOR: "Доктор", SHERIFF: "Комиссар",
    LOVER: "Путана", VOYEUR: "Вуайерист", STRIPPER: "Стриптизёрша", MANIAC: "Маньяк",
    WIFE: "Ревнивая жена", CIVIL: "Мирный житель",
}

# Ночные действия (kind)
KILL, DON_CHECK, HEAL, CHECK, VISIT, MANIAC_KILL = "kill", "doncheck", "heal", "check", "visit", "maniac"
WATCH, DANCE, BOOZE = "watch", "dance", "booze"


def role_plan(n):
    """Набор ролей на n игроков. Путана — с 7, Вуайерист — с 8, Маньяк — с 9,
    Стриптизёрша — с 10, Ревнивая жена — с 11, Бармен (в мафии) — с 12."""
    if not MIN_PLAYERS <= n <= MAX_PLAYERS:
        raise ValueError(f"нужно {MIN_PLAYERS}–{MAX_PLAYERS} игроков, а не {n}")
    mafia_total = 1 if n <= 6 else 2 if n <= 9 else 3 if n <= 12 else 4
    if mafia_total == 1:
        roles = [MAFIA]
    else:
        roles = [DON] + ([BARTENDER] if n >= 12 else [])
        roles += [MAFIA] * (mafia_total - len(roles))
    roles += [DOCTOR, SHERIFF]
    for need, role in ((7, LOVER), (8, VOYEUR), (9, MANIAC), (10, STRIPPER), (11, WIFE)):
        if n >= need:
            roles.append(role)
    return roles + [CIVIL] * (n - len(roles))


def new_game(players, rng=random):
    """players: {uid: name} в порядке записи. Возвращает состояние с розданными ролями."""
    ids = list(players)
    roles = role_plan(len(ids))
    rng.shuffle(roles)
    st = {
        "players": {u: {"name": players[u], "role": r, "alive": True} for u, r in zip(ids, roles)},
        "memory": {"last_heal": None, "self_healed": False, "last_visit": None, "last_dance": None, "silenced": None},
        "night": {"acts": {}},
        "votes": {},
        "husband": None,
    }
    wife = next((u for u in ids if st["players"][u]["role"] == WIFE), None)
    if wife:
        st["husband"] = rng.choice([u for u in ids if u != wife])
    return st


def alive(st):
    return [u for u, p in st["players"].items() if p["alive"]]


def role_of(st, uid):
    return st["players"][uid]["role"]


def alive_with(st, roles):
    return [u for u in alive(st) if role_of(st, u) in roles]


def is_mafia(st, uid):
    return role_of(st, uid) in MAFIA_TEAM


# ---------- ночь ----------

def start_night(st):
    st["night"] = {"acts": {}}
    st["memory"]["silenced"] = None  # Стриптизёрша действует только на один день


def night_tasks(st):
    """Кто что должен сделать этой ночью: [(uid, kind, [допустимые цели])]."""
    mem = st["memory"]
    a = alive(st)
    civ_targets = [t for t in a if not is_mafia(st, t)]
    tasks = []
    for u in a:
        r = role_of(st, u)
        others = [t for t in a if t != u]
        if r in MAFIA_TEAM:
            tasks.append((u, KILL, civ_targets))
        if r == DON:
            tasks.append((u, DON_CHECK, civ_targets))
        elif r == BARTENDER:
            tasks.append((u, BOOZE, civ_targets))
        elif r == DOCTOR:
            tasks.append((u, HEAL, [t for t in a if t != mem["last_heal"] and (t != u or not mem["self_healed"])]))
        elif r == SHERIFF:
            tasks.append((u, CHECK, others))
        elif r == LOVER:
            tasks.append((u, VISIT, [t for t in others if t != mem["last_visit"]]))
        elif r == VOYEUR:
            tasks.append((u, WATCH, others))
        elif r == STRIPPER:
            tasks.append((u, DANCE, [t for t in others if t != mem.get("last_dance")]))
        elif r == MANIAC:
            tasks.append((u, MANIAC_KILL, others))
    return [t for t in tasks if t[2]]  # без целей действовать нечем


def record_action(st, uid, kind, target):
    for u, k, targets in night_tasks(st):
        if u == uid and k == kind:
            if target not in targets:
                return False
            st["night"]["acts"][f"{uid}:{kind}"] = target
            return True
    return False


def night_done(st):
    acts = st["night"]["acts"]
    return all(f"{u}:{k}" in acts for u, k, _ in night_tasks(st))


def _single(acts, kind):
    for key, target in acts.items():
        u, k = key.split(":")
        if k == kind:
            return u, target
    return None, None


def resolve_night(st):
    """Применяет ночь. Порядок: Путана блокирует гостя → Бармен подпаивает → убийства →
    ревность жены → лечение → проверки и подглядывание → танец Стриптизёрши.

    Возвращает {"dead", "saved", "blocked", "checks": [(кто, кого, kind, результат)],
                "watch": (вуайерист, за кем, [кто приходил]) | None, "silenced", "jealous": путана|None}.
    """
    acts = st["night"]["acts"]
    mem = st["memory"]
    works = lambda actor: actor is not None and actor != blocked  # noqa: E731

    lover, guest = _single(acts, VISIT)
    blocked = guest  # у гостя Путаны ночное действие не срабатывает

    bartender, boozed = _single(acts, BOOZE)
    if not works(bartender):
        boozed = None

    votes = [(k.split(":")[0], t) for k, t in acts.items() if k.endswith(":" + KILL) and works(k.split(":")[0])]
    kill = None
    if votes:
        count = Counter(t for _, t in votes)
        top = max(count.values())
        leaders = [t for t, v in count.items() if v == top]  # порядок — по первому голосу
        don_vote = [t for u, t in votes if role_of(st, u) == DON]
        kill = don_vote[0] if len(leaders) > 1 and don_vote and don_vote[0] in leaders else leaders[0]

    maniac, maniac_target = _single(acts, MANIAC_KILL)
    if not works(maniac):
        maniac_target = None

    # Ревнивая жена: Путана у её мужа — жена расправляется с Путаной
    jealous = None
    wives = alive_with(st, {WIFE})
    if lover and guest and wives and guest == st.get("husband"):
        jealous = lover

    doctor, heal = _single(acts, HEAL)
    if not works(doctor) or doctor == boozed:  # пьяный доктор не вылечит
        heal = None

    dead, saved = [], []
    for t in dict.fromkeys(x for x in (kill, maniac_target, jealous) if x):
        (saved if t == heal else dead).append(t)

    checks = []
    don, don_target = _single(acts, DON_CHECK)
    if works(don):
        checks.append((don, don_target, DON_CHECK, role_of(st, don_target) == SHERIFF))
    sheriff, sheriff_target = _single(acts, CHECK)
    if works(sheriff):
        truth = is_mafia(st, sheriff_target)
        checks.append((sheriff, sheriff_target, CHECK, (not truth) if sheriff == boozed else truth))

    watch = None
    voyeur, watched = _single(acts, WATCH)
    if works(voyeur):
        visitors = [k.split(":")[0] for k, t in acts.items() if t == watched and k.split(":")[0] not in (voyeur, watched)]
        watch = (voyeur, watched, list(dict.fromkeys(visitors)))

    stripper, dancer_guest = _single(acts, DANCE)
    silenced = dancer_guest if works(stripper) and dancer_guest not in dead else None

    for d in dead:
        st["players"][d]["alive"] = False
    if doctor:
        mem["last_heal"] = acts.get(f"{doctor}:{HEAL}")
        if mem["last_heal"] == doctor:
            mem["self_healed"] = True
    else:
        mem["last_heal"] = None
    mem["last_visit"] = guest
    mem["last_dance"] = dancer_guest
    mem["silenced"] = silenced
    return {"dead": dead, "saved": saved, "blocked": blocked, "checks": checks, "watch": watch,
            "silenced": silenced, "jealous": jealous if jealous in dead else None}


# ---------- день ----------

SKIP = "skip"


def voters(st):
    """Кто голосует сегодня: живые, кроме гостя Стриптизёрши."""
    return [u for u in alive(st) if u != st["memory"].get("silenced")]


def start_vote(st):
    st["votes"] = {}


def record_vote(st, voter, target):
    if voter not in voters(st) or (target != SKIP and (target not in alive(st) or target == voter)):
        return False
    st["votes"][voter] = target
    return True


def vote_done(st):
    return all(u in st["votes"] for u in voters(st))


def tally(st):
    return Counter(st["votes"].values())


def resolve_vote(st):
    """Казнят единственного лидера, если за него больше голосов, чем за «пропустить»."""
    count = tally(st)
    skips = count.pop(SKIP, 0)
    if not count:
        return None
    top = max(count.values())
    leaders = [t for t, v in count.items() if v == top]
    if len(leaders) > 1 or top <= skips:
        return None
    st["players"][leaders[0]]["alive"] = False
    return leaders[0]


# ---------- победа ----------

def winner(st):
    """None — игра продолжается; иначе 'town' | 'mafia' | 'maniac' | 'draw'."""
    a = alive(st)
    m = len(alive_with(st, MAFIA_TEAM))
    man = len(alive_with(st, {MANIAC}))
    if not a:
        return "draw"
    if m == 0 and man == 0:
        return "town"
    if m and m >= len(a) - m:
        return "mafia"
    if man and m == 0 and len(a) <= 2:
        return "maniac"
    return None


def wife_won(st):
    """Ревнивая жена побеждает вместе с любым исходом, если дожила до конца вместе с мужем."""
    wife = next((u for u, p in st["players"].items() if p["role"] == WIFE), None)
    h = st.get("husband")
    return bool(wife and h and st["players"][wife]["alive"] and st["players"][h]["alive"])
