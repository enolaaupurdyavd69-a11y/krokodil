"""Картинки Мафии для Telegram: ночь, утро с табло города, приговор, финал, карточка роли.

Рисуются на ПК через Pillow в стиле сайта игры (тёмный фон, неон). Возвращают PNG-байты.
"""
import io
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

FONTS = Path("C:/Windows/Fonts")
BOLD, REGULAR, EMOJI = FONTS / "segoeuib.ttf", FONTS / "segoeui.ttf", FONTS / "seguiemj.ttf"

GROUND, SCREEN, RAISE, LINE = (20, 15, 31), (29, 21, 48), (42, 31, 68), (58, 45, 92)
FG, MUTED = (243, 238, 252), (169, 156, 198)
PINK, AMBER, OK, BAD, BLUE, VIOLET = (255, 79, 163), (255, 181, 71), (61, 220, 151), (255, 107, 107), (42, 171, 238), (179, 136, 255)

ROLE_STYLE = {  # иконка и цвет роли
    "mafia": ("🔪", BAD), "don": ("🎩", AMBER), "doctor": ("💉", OK), "sheriff": ("🚓", BLUE),
    "lover": ("💋", PINK), "maniac": ("🪓", (255, 122, 69)), "civil": ("🙂", VIOLET),
}
AVATAR_COLORS = [PINK, AMBER, BLUE, OK, VIOLET, (255, 122, 69), (94, 214, 214), (240, 98, 146)]

_fonts = {}


def font(size, kind="bold"):
    key = (kind, size)
    if key not in _fonts:
        path = {"bold": BOLD, "regular": REGULAR, "emoji": EMOJI}[kind]
        try:
            _fonts[key] = ImageFont.truetype(str(path), size)
        except OSError:  # не Windows — без красивых шрифтов, но не падаем
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def clean(name):
    """Эмодзи и редкие символы из имён Telegram шрифт текста не рисует — убираем их."""
    s = "".join(ch for ch in name if ord(ch) < 0x2000 and ch.isprintable()).strip()
    return s or "Игрок"


def png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def canvas(w, h, glow=PINK):
    """Фон: вертикальный градиент и мягкое цветное свечение сверху."""
    im = Image.new("RGB", (w, h), GROUND)
    px = ImageDraw.Draw(im)
    for y in range(h):
        k = y / h
        px.line([(0, y), (w, y)], fill=tuple(int(SCREEN[i] * (1 - k) + GROUND[i] * k) for i in range(3)))
    glow_im = Image.new("RGB", (w, h), (0, 0, 0))
    g = ImageDraw.Draw(glow_im)
    for r in range(int(w * .45), 0, -12):
        a = (1 - r / (w * .45)) * .22
        g.ellipse([w * .72 - r, -r * .6, w * .72 + r, r * .6], fill=tuple(int(c * a) for c in glow))
    return ImageChops.add(im, glow_im)


def emoji(im, xy, ch, size, anchor="mm"):
    ImageDraw.Draw(im).text(xy, ch, font=font(size, "emoji"), embedded_color=True, anchor=anchor)


def text(d, xy, s, size, fill=FG, kind="bold", anchor="la"):
    d.text(xy, s, font=font(size, kind), fill=fill, anchor=anchor)


def wrap(s, size, width, kind="regular"):
    f, lines, cur = font(size, kind), [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if f.getlength(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def dim(c, k=.35):
    return tuple(int(v * k + GROUND[i] * (1 - k)) for i, v in enumerate(c))


def avatar(im, cx, cy, r, name, idx, dead=False):
    d = ImageDraw.Draw(im)
    color = AVATAR_COLORS[idx % len(AVATAR_COLORS)]
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=dim(color, .25) if dead else color)
    text(d, (cx, cy), clean(name)[:1].upper(), int(r * 1.05), fill=MUTED if dead else GROUND, anchor="mm")
    if dead:
        emoji(im, (cx + r * .7, cy + r * .7), "💀", int(r * .8))


# ---------- табло города ----------

def board(im, top, players, reveal_all=False, mark=(), role_names=None):
    """Сетка игроков: живые цветные, мёртвые серые с раскрытой ролью; mark — подсветить (погибшие сейчас)."""
    w = im.width
    cols = 4 if len(players) > 9 else 3 if len(players) > 4 else len(players)
    pad, gap = 48, 18
    tw = (w - pad * 2 - gap * (cols - 1)) / cols
    th = 118
    d = ImageDraw.Draw(im)
    for i, (uid, p) in enumerate(players.items()):
        x = pad + (i % cols) * (tw + gap)
        y = top + (i // cols) * (th + gap)
        dead = not p["alive"]
        border = BAD if uid in mark else LINE
        d.rounded_rectangle([x, y, x + tw, y + th], 20, fill=SCREEN if dead else RAISE, outline=border, width=4 if uid in mark else 2)
        avatar(im, int(x + 56), int(y + th / 2), 36, p["name"], i, dead)
        name = clean(p["name"])
        f = font(30)
        while f.getlength(name) > tw - 120 and len(name) > 3:
            name = name[:-2] + "…"
        text(d, (x + 108, y + 26), name, 30, fill=MUTED if dead else FG)
        if dead or reveal_all:
            icon, color = ROLE_STYLE[p["role"]]
            emoji(im, (x + 122, y + 82), icon, 26)
            text(d, (x + 142, y + 82), role_names[p["role"]], 24, fill=color, kind="regular", anchor="lm")
        else:
            text(d, (x + 108, y + 82), "в игре", 24, fill=OK, kind="regular", anchor="lm")
    rows = (len(players) + cols - 1) // cols
    return top + rows * (th + gap)


def board_height(n):
    cols = 4 if n > 9 else 3 if n > 4 else n
    return ((n + cols - 1) // cols) * (118 + 18)


# ---------- карточки ----------

NIGHT_LINES = ["Город засыпает", "Шторы задёрнуты", "Свет гаснет", "Бары закрылись"]


def night(n, alive_count):
    im = canvas(1280, 520, VIOLET)
    d = ImageDraw.Draw(im)
    emoji(im, (230, 260), "🌙", 220)
    text(d, (420, 150), f"Ночь {n}", 128)
    text(d, (426, 310), random.choice(NIGHT_LINES), 46, fill=MUTED, kind="regular")
    text(d, (426, 380), f"Живых: {alive_count} · мафия, проверьте личку", 34, fill=PINK, kind="regular")
    return png(im)


def morning(day, players, dead_now, role_names, saved=False):
    head = 300
    im = canvas(1280, head + board_height(len(players)) + 40, AMBER)
    d = ImageDraw.Draw(im)
    emoji(im, (140, 150), "🌅", 150)
    text(d, (260, 70), f"Утро {day}", 96)
    if dead_now:
        names = ", ".join(f"{clean(players[u]['name'])} ({role_names[players[u]['role']]})" for u in dead_now)
        lines = wrap("Ночью погибли: " + names if len(dead_now) > 1 else "Ночью погиб(ла): " + names, 38, 960)
        for i, line in enumerate(lines[:2]):
            text(d, (266, 190 + i * 46), line, 38, fill=BAD, kind="regular")
    else:
        text(d, (266, 196), "Доктор успел — все живы" if saved else "Ночь прошла спокойно — все живы", 38, fill=OK, kind="regular")
    board(im, head, players, mark=set(dead_now), role_names=role_names)
    return png(im)


def verdict(players, executed, role_names):
    head = 300
    im = canvas(1280, head + board_height(len(players)) + 40, BAD if executed else BLUE)
    d = ImageDraw.Draw(im)
    emoji(im, (140, 150), "🔨", 150)
    text(d, (260, 70), "Приговор", 96)
    if executed:
        p = players[executed]
        text(d, (266, 196), f"Казнён(а): {clean(p['name'])} — {role_names[p['role']]}", 38, fill=BAD, kind="regular")
    else:
        text(d, (266, 196), "Город не договорился — никого не казнили", 38, fill=MUTED, kind="regular")
    board(im, head, players, mark={executed} if executed else set(), role_names=role_names)
    return png(im)


WIN_STYLE = {
    "town": ("🎉", "Победили мирные", OK), "mafia": ("🔪", "Победила мафия", BAD),
    "maniac": ("🪓", "Победил маньяк", (255, 122, 69)), "draw": ("🌫", "Ничья", MUTED),
    "stop": ("⛔", "Партия остановлена", MUTED),
}


def final(players, winner, role_names):
    icon, title, color = WIN_STYLE[winner]
    head = 300
    im = canvas(1280, head + board_height(len(players)) + 40, color)
    d = ImageDraw.Draw(im)
    emoji(im, (150, 150), icon, 160)
    text(d, (290, 80), title, 92, fill=color)
    text(d, (296, 200), "Все роли раскрыты", 38, fill=MUTED, kind="regular")
    board(im, head, players, reveal_all=True, role_names=role_names)
    return png(im)


def role_card(role, role_name, description, goal, title, mates=()):
    icon, color = ROLE_STYLE[role]
    im = canvas(900, 1100, color)
    d = ImageDraw.Draw(im)
    text(d, (450, 70), f"Мафия 18+ · {clean(title)}", 30, fill=MUTED, kind="regular", anchor="ma")
    d.ellipse([300, 150, 600, 450], fill=dim(color, .3), outline=color, width=6)
    emoji(im, (450, 300), icon, 170)
    text(d, (450, 500), role_name, 78, fill=color, anchor="ma")
    y = 620
    for line in wrap(description, 34, 760):
        text(d, (450, y), line, 34, fill=FG, kind="regular", anchor="ma")
        y += 46
    y += 20
    for line in wrap(goal, 30, 760):
        text(d, (450, y), line, 30, fill=MUTED, kind="regular", anchor="ma")
        y += 40
    if mates:
        y += 24
        for line in wrap("Ваша семья: " + ", ".join(clean(m) for m in mates), 32, 760, "bold"):
            text(d, (450, y), line, 32, fill=BAD, anchor="ma")
            y += 42
    return png(im.crop((0, 0, 900, min(1100, y + 70))))  # без пустого низа
