"""Логика Мафии без Telegram: роли, ночь, голосование, победа.

Состояние партии — обычный dict (бот хранит его в SQLite как JSON), id игроков — строки.
players: {uid: {"name": str, "role": str, "alive": bool}}
"""
import random
from collections import Counter

MAFIA, DON, DOCTOR, SHERIFF, LOVER, MANIAC, CIVIL = "mafia", "don", "doctor", "sheriff", "lover", "maniac", "civil"
MAFIA_TEAM = {MAFIA, DON}
MIN_PLAYERS, MAX_PLAYERS = 5, 15

ROLE_NAMES = {
    MAFIA: "Мафия", DON: "Дон мафии", DOCTOR: "Доктор", SHERIFF: "Комиссар",
    LOVER: "Любовница", MANIAC: "Маньяк", CIVIL: "Мирный житель",
}

# Ночные действия: kind -> какая роль его делает
KILL, DON_CHECK, HEAL, CHECK, VISIT, MANIAC_KILL = "kill", "doncheck", "heal", "check", "visit", "maniac"


def role_plan(n):
    """Набор ролей на n игроков. Любовница — с 7 игроков, Маньяк — с 9."""
    if not MIN_PLAYERS <= n <= MAX_PLAYERS:
        raise ValueError(f"нужно {MIN_PLAYERS}–{MAX_PLAYERS} игроков, а не {n}")
    mafia_total = 1 if n <= 6 else 2 if n <= 9 else 3 if n <= 12 else 4
    roles = [DON] + [MAFIA] * (mafia_total - 1) if mafia_total >= 2 else [MAFIA]
    roles += [DOCTOR, SHERIFF]
    if n >= 7:
        roles.append(LOVER)
    if n >= 9:
        roles.append(MANIAC)
    return roles + [CIVIL] * (n - len(roles))


def new_game(players, rng=random):
    """players: {uid: name} в порядке записи. Возвращает состояние с розданными ролями."""
    ids = list(players)
    roles = role_plan(len(ids))
    rng.shuffle(roles)
    return {
        "players": {u: {"name": players[u], "role": r, "alive": True} for u, r in zip(ids, roles)},
        "memory": {"last_heal": None, "self_healed": False, "last_visit": None},
        "night": {"acts": {}},
        "votes": {},
    }


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


def night_tasks(st):
    """Кто что должен сделать этой ночью: [(uid, kind, [допустимые цели])]."""
    mem = st["memory"]
    a = alive(st)
    civ_targets = [t for t in a if not is_mafia(st, t)]
    tasks = []
    for u in a:
        r = role_of(st, u)
        if r in MAFIA_TEAM:
            tasks.append((u, KILL, civ_targets))
        if r == DON:
            tasks.append((u, DON_CHECK, civ_targets))
        elif r == DOCTOR:
            tasks.append((u, HEAL, [t for t in a if t != mem["last_heal"] and (t != u or not mem["self_healed"])]))
        elif r == SHERIFF:
            tasks.append((u, CHECK, [t for t in a if t != u]))
        elif r == LOVER:
            tasks.append((u, VISIT, [t for t in a if t != u and t != mem["last_visit"]]))
        elif r == MANIAC:
            tasks.append((u, MANIAC_KILL, [t for t in a if t != u]))
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
    """Применяет ночь. Порядок: Любовница блокирует гостя → убийства → лечение → проверки.

    Возвращает {"dead": [...], "saved": [...], "blocked": uid|None,
                "checks": [(кто, кого, kind, результат_bool)]}.
    """
    acts = st["night"]["acts"]
    mem = st["memory"]

    lover, guest = _single(acts, VISIT)
    blocked = guest  # у гостя Любовницы ночное действие не срабатывает

    votes = [(k.split(":")[0], t) for k, t in acts.items() if k.endswith(":" + KILL) and k.split(":")[0] != blocked]
    kill = None
    if votes:
        count = Counter(t for _, t in votes)
        top = max(count.values())
        leaders = [t for t, v in count.items() if v == top]  # порядок — по первому голосу
        don_vote = [t for u, t in votes if role_of(st, u) == DON]
        kill = don_vote[0] if len(leaders) > 1 and don_vote and don_vote[0] in leaders else leaders[0]

    maniac, maniac_target = _single(acts, MANIAC_KILL)
    if maniac == blocked:
        maniac_target = None
    doctor, heal = _single(acts, HEAL)
    if doctor == blocked:
        heal = None

    dead, saved = [], []
    for t in dict.fromkeys(x for x in (kill, maniac_target) if x):
        (saved if t == heal else dead).append(t)

    checks = []
    don, don_target = _single(acts, DON_CHECK)
    if don and don != blocked:
        checks.append((don, don_target, DON_CHECK, role_of(st, don_target) == SHERIFF))
    sheriff, sheriff_target = _single(acts, CHECK)
    if sheriff and sheriff != blocked:
        checks.append((sheriff, sheriff_target, CHECK, is_mafia(st, sheriff_target)))

    for d in dead:
        st["players"][d]["alive"] = False
    if doctor:
        mem["last_heal"] = acts.get(f"{doctor}:{HEAL}")
        if mem["last_heal"] == doctor:
            mem["self_healed"] = True
    else:
        mem["last_heal"] = None
    mem["last_visit"] = guest
    return {"dead": dead, "saved": saved, "blocked": blocked, "checks": checks}


# ---------- день ----------

SKIP = "skip"


def start_vote(st):
    st["votes"] = {}


def record_vote(st, voter, target):
    a = alive(st)
    if voter not in a or (target != SKIP and (target not in a or target == voter)):
        return False
    st["votes"][voter] = target
    return True


def vote_done(st):
    return all(u in st["votes"] for u in alive(st))


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
