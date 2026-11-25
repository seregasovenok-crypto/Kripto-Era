"""Крипто эра: вертикальное видео (9:16) для TikTok / Reels / Shorts из новостей канала.

Шаги конвейера:
  1. Новости: берём последние опубликованные в канале новости (state.json → posted),
     если их мало, добираем свежие из русскоязычных RSS.
  2. Сценарий: интро-«крючок», N новостей (заголовок + 1–2 предложения), призыв подписаться.
  3. Озвучка: edge-tts (бесплатные нейроголоса Microsoft), запасной вариант gTTS,
     если оба недоступны, видео собирается без голоса.
  4. Картинки: генерируются кодом (Pillow) под тему новости: BTC, ETH, взлом, регулирование,
     рост/падение рынка и т.д. Никаких чужих фото и логотипов, поэтому никаких проблем с правами.
  5. Субтитры: крупные, по 2–4 слова, синхронизированы с голосом.
  6. Сборка: ffmpeg, 1080×1920, 30 fps, H.264 + AAC; плавный зум фона.
  7. Отправка: готовый ролик и текст описания приходят в личку от бота (TELEGRAM_VIDEO_CHAT),
     а также сохраняются как артефакт GitHub Actions.
"""
import asyncio
import calendar
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1080, 1920, 30
MSK = timezone(timedelta(hours=3))
STATE_FILE = "state.json"
OUT_DIR = os.environ.get("VIDEO_OUT", "out")
N_NEWS = int(os.environ.get("VIDEO_NEWS", "3"))
VOICE = os.environ.get("VIDEO_VOICE", "ru-RU-DmitryNeural")
VOICE_RATE = os.environ.get("VIDEO_VOICE_RATE", "+8%")
NEWS_MAX_AGE_HOURS = 26
UA = "Mozilla/5.0 (compatible; KriptoEraBot/1.0)"
RU_FEEDS = [
    ("ForkLog", "https://forklog.com/feed"),
    ("Bits.media", "https://bits.media/rss2/"),
    ("BeInCrypto", "https://ru.beincrypto.com/feed/"),
]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

FONT_BOLD = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
FONT_REG = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_SYMBOL = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]

# тема → (ключевые слова, основной цвет, символ)
THEMES = [
    ("hack", r"взлом|хакер|эксплойт|украл|похитил|мошенн|скам|hack|exploit|stolen|scam", (230, 57, 70), "⚠"),
    ("law", r"\bsec\b|регулир|закон|суд|налог|запрет|минфин|госдум|цб\b|центробанк|ес\b|mica|штраф", (88, 140, 230), "⚖"),
    ("etf", r"\betf\b|фонд|blackrock|fidelity|grayscale", (46, 204, 113), "ETF"),
    ("btc", r"биткоин|биткойн|bitcoin|\bbtc\b|майн", (247, 147, 26), "BTC"),
    ("eth", r"ethereum|эфир|\beth\b|виталик|бутерин", (130, 120, 255), "Ξ"),
    ("sol", r"solana|солан|\bsol\b", (153, 69, 255), "◎"),
    ("ton", r"\bton\b|toncoin|тонкоин|telegram|телеграм|дуров", (0, 152, 234), "TON"),
    ("xrp", r"\bxrp\b|ripple", (60, 180, 210), "XRP"),
    ("stable", r"стейблкоин|stablecoin|usdt|usdc|tether|цифровой рубл", (38, 161, 123), "$"),
]
DEFAULT_THEME = ("crypto", "", (0, 200, 170), "₿")
UP = r"вырос|рост|растет|растёт|подорож|рекорд|максимум|ралли|взлет|взлёт|прибыл|приток"
DOWN = r"упал|паден|снизил|сниж|обвал|ликвидац|минимум|отток|убыт|просел|коррекц|трудност"


# ---------- 1. Новости ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"(The post|Сообщение) .*? (appeared first on|появились сначала на).*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_rss(limit):
    try:
        import feedparser
    except ImportError:
        return []
    items = []
    for source, url in RU_FEEDS:
        try:
            feed = feedparser.parse(url, agent=UA)
        except Exception as e:  # noqa: BLE001
            print("Ошибка ленты:", source, e)
            continue
        for entry in feed.entries[:5]:
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            items.append({
                "title": clean(entry.get("title", "")),
                "summary": clean(entry.get("summary", ""))[:400],
                "source": source,
                "link": entry.get("link", ""),
                "ts": calendar.timegm(t) if t else 0,
            })
    items.sort(key=lambda x: -x["ts"])
    return items[: limit * 3]


def gather_news():
    path = os.environ.get("VIDEO_NEWS_FILE")
    if path:  # ручной набор новостей (для теста или особого выпуска)
        with open(path, encoding="utf-8") as f:
            return json.load(f)[:N_NEWS]
    now = time.time()
    posted = [p for p in load_state().get("posted", [])
              if now - p.get("ts", 0) < NEWS_MAX_AGE_HOURS * 3600]
    posted.sort(key=lambda p: -p.get("ts", 0))
    news = posted[:N_NEWS]
    if len(news) < N_NEWS:
        titles = {n["title"] for n in news}
        for item in fetch_rss(N_NEWS):
            if len(news) >= N_NEWS:
                break
            if item["title"] and item["title"] not in titles:
                news.append(item)
                titles.add(item["title"])
    return news


# ---------- 2. Сценарий ----------

def short_summary(summary, title, limit=230):
    """1–2 законченных предложения из анонса, без обрывков и повтора заголовка."""
    s = re.sub(r"\s*\[?…\]?$|\s*\.\.\.$", "", summary or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", s)
    out = ""
    for sent in sentences:
        if not re.search(r"[.!?]$", sent):
            break  # обрезанное предложение
        if sent.strip(" .").lower() == title.strip(" .").lower():
            continue
        if len(out) + len(sent) > limit:
            break
        out = f"{out} {sent}".strip()
    return out


def pick_theme(text):
    for name, pattern, color, symbol in THEMES:
        if re.search(pattern, text, re.IGNORECASE):
            return name, color, symbol
    return DEFAULT_THEME[0], DEFAULT_THEME[2], DEFAULT_THEME[3]


def trend(text):
    up, down = re.search(UP, text, re.I), re.search(DOWN, text, re.I)
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0


def for_voice(text):
    """Подготовка текста к озвучке: убираем кавычки-ёлочки и символы, которые голос читает плохо."""
    text = text.replace("«", "").replace("»", "").replace(" ", " ")
    text = re.sub(r"\$(\d[\d\s,.]*)\s*(млрд|млн|тыс\.?)?", lambda m: f"{m.group(1).strip()} {m.group(2) or ''} долларов", text)
    text = text.replace("%", " процентов")
    return re.sub(r"\s+", " ", text).strip()


def build_script(news, date):
    day = f"{date.day} {MONTHS[date.month - 1]}"
    n = len(news)
    word = {1: "главная новость", 2: "главные новости", 3: "главные новости", 4: "главные новости"}.get(n, "главных новостей")
    segments = [{
        "kind": "intro",
        "voice": f"{n} {word} крипты за {day}. Смотри до конца!",
        "title": f"{n} {word.upper()} КРИПТЫ",
        "theme": ("intro", (0, 200, 170), "₿"),
        "trend": 0,
        "date": day,
    }]
    for i, item in enumerate(news, 1):
        title = item["title"].strip().rstrip(".")
        summ = short_summary(item.get("summary", ""), title)
        text = f"{title}. {summ}".strip()
        segments.append({
            "kind": "news",
            "index": i,
            "total": n,
            "voice": text,
            "title": title,
            "source": item.get("source", ""),
            "theme": pick_theme(title) if pick_theme(title)[0] != "crypto" else pick_theme(f"{title} {summ}"),
            "trend": trend(f"{title} {summ}"),
        })
    segments.append({
        "kind": "outro",
        "voice": "Подписывайся, чтобы не пропускать главное. А все новости в течение дня — в нашем Телеграм-канале.",
        "title": "БОЛЬШЕ НОВОСТЕЙ",
        "theme": ("outro", (0, 152, 234), "TG"),
        "trend": 1,
    })
    return segments


def build_caption(news, date):
    day = f"{date.day} {MONTHS[date.month - 1]}"
    lines = [f"Главное в крипте за {day} 🔥", ""]
    for i, item in enumerate(news, 1):
        lines.append(f"{i}. {item['title'].strip().rstrip('.')}")
    tags = ["#крипта", "#криптовалюта", "#новости", "#биткоин", "#bitcoin", "#инвестиции", "#криптоэра"]
    text = " ".join(item["title"] for item in news)
    for tag, pattern in (("#эфириум", r"эфир|ethereum"), ("#ton", r"\bton\b|тонкоин|telegram"),
                         ("#solana", r"solana|солан"), ("#etf", r"\betf\b")):
        if re.search(pattern, text, re.I):
            tags.append(tag)
    channel = os.environ.get("TELEGRAM_CHANNEL", "")
    if channel.startswith("@"):
        lines += ["", f"Telegram: {channel}"]
    lines += ["", "Не является финансовой рекомендацией.", "", " ".join(tags)]
    return "\n".join(lines)


# ---------- 3. Озвучка ----------

def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


async def _edge(text, path):
    import edge_tts
    proxy = os.environ.get("HTTPS_PROXY") or None
    comm = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, proxy=proxy, boundary="WordBoundary")
    words = []
    with open(path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7, chunk["text"]))
    return words


def tts(text, path):
    """Возвращает (путь к аудио или None, список слов с таймингом или None)."""
    try:
        words = asyncio.run(asyncio.wait_for(_edge(text, path), timeout=60))
        if os.path.getsize(path) > 1000:
            return path, words or None
    except Exception as e:  # noqa: BLE001
        print("edge-tts недоступен:", str(e)[:120])
    try:
        from gtts import gTTS
        gTTS(text, lang="ru").save(path)
        if os.path.getsize(path) > 1000:
            return path, None
    except Exception as e:  # noqa: BLE001
        print("gTTS недоступен:", str(e)[:120])
    return None, None


# ---------- 4. Картинки ----------

def font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def wrap(draw, text, fnt, max_w, max_lines=None):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=fnt) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(",.;:") + "…"
    return lines


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def draw_symbol(img, cx, cy, r, color, symbol):
    """Светящаяся «монета» с символом темы (рисуем сами, без чужих логотипов)."""
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((cx - r * 1.5, cy - r * 1.5, cx + r * 1.5, cy + r * 1.5), fill=color + (110,))
    glow = glow.filter(ImageFilter.GaussianBlur(r * 0.45))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=mix(color, (20, 20, 30), 0.15) + (255,))
    d.ellipse((cx - r + 18, cy - r + 18, cx + r - 18, cy + r - 18), outline=(255, 255, 255, 90), width=6)
    if symbol == "₿":  # в DejaVu нет ₿: рисуем B с двумя штрихами
        f = font(FONT_BOLD, int(r * 1.15))
        d.text((cx, cy), "B", font=f, fill="white", anchor="mm")
        bw, bh = int(r * 0.07), int(r * 0.22)
        for dx in (-r * 0.12, r * 0.08):
            x = cx + dx
            d.rectangle((x - bw / 2, cy - r * 0.62 - bh / 2, x + bw / 2, cy - r * 0.42), fill="white")
            d.rectangle((x - bw / 2, cy + r * 0.42, x + bw / 2, cy + r * 0.62 + bh / 2), fill="white")
    else:
        size = int(r * (1.1 if len(symbol) == 1 else 0.62 if len(symbol) == 2 else 0.5))
        f = font(FONT_SYMBOL if len(symbol) == 1 else FONT_BOLD, size)
        d.text((cx, cy), symbol, font=f, fill="white", anchor="mm")


def draw_chart(d, rnd, color, direction, top, bottom):
    """Свечной график на фоне: вверх, вниз или боковик."""
    n, x0, step = 22, 40, (W - 80) / 22
    price, prices = 0.5, []
    for i in range(n):
        drift = direction * 0.025
        o = price
        c = min(0.95, max(0.05, o + drift + rnd.uniform(-0.06, 0.06)))
        hi, lo = max(o, c) + rnd.uniform(0, 0.03), min(o, c) - rnd.uniform(0, 0.03)
        prices.append((o, c, hi, lo))
        price = c
    if direction:  # подгоняем, чтобы тренд был виден
        first, last = prices[0][0], prices[-1][1]
        if (last - first) * direction < 0.25:
            shift = (0.25 * direction - (last - first)) / n
            prices = [tuple(v + shift * i for v in p) for i, p in enumerate(prices)]
    lo_all = min(p[3] for p in prices)
    hi_all = max(p[2] for p in prices)

    def y(v):
        return bottom - (v - lo_all) / (hi_all - lo_all + 1e-9) * (bottom - top)

    green, red = (46, 204, 113, 150), (231, 76, 60, 150)
    for i, (o, c, hi, lo) in enumerate(prices):
        x = x0 + i * step + step / 2
        col = green if c >= o else red
        d.line((x, y(hi), x, y(lo)), fill=col, width=4)
        d.rectangle((x - step * 0.3, min(y(o), y(c)), x + step * 0.3, max(y(o), y(c)) + 2), fill=col)


def background(seg, seed):
    name, color, symbol = seg["theme"]
    rnd = random.Random(seed)
    base_top, base_bot = (8, 10, 22), mix(color, (5, 6, 14), 0.78)
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for yy in range(H):
        d.line((0, yy, W, yy), fill=mix(base_top, base_bot, yy / H) + (255,))
    # сетка
    for x in range(0, W, 90):
        d.line((x, 0, x, H), fill=(255, 255, 255, 12))
    for yy in range(0, H, 90):
        d.line((0, yy, W, yy), fill=(255, 255, 255, 12))
    # «сеть блокчейна»: точки и связи
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    pts = [(rnd.uniform(0, W), rnd.uniform(0, H)) for _ in range(38)]
    for i, p in enumerate(pts):
        for q in pts[i + 1:]:
            if math.dist(p, q) < 260:
                ld.line((p, q), fill=color + (45,), width=2)
        rr = rnd.uniform(3, 7)
        ld.ellipse((p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr), fill=color + (120,))
    draw_chart(ld, rnd, color, seg.get("trend", 0), 1180, 1560)
    img.alpha_composite(layer)
    draw_symbol(img, W // 2, 980, 190, color, symbol)
    # затемнение сверху и снизу для читаемости текста
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for yy in range(H):
        a = 0
        if yy < 760:
            a = int(150 * (1 - yy / 760))
        elif yy > 1500:
            a = int(170 * (yy - 1500) / (H - 1500))
        sd.line((0, yy, W, yy), fill=(0, 0, 0, a))
    img.alpha_composite(shade)
    return img.convert("RGB")


def pill(d, x, y, text, fnt, fill, color="white", pad=(26, 12), anchor_right=False):
    tw = d.textlength(text, font=fnt)
    asc, desc = fnt.getmetrics()
    w, h = tw + pad[0] * 2, asc + desc + pad[1] * 2
    if anchor_right:
        x -= w
    d.rounded_rectangle((x, y, x + w, y + h), radius=h / 2, fill=fill)
    d.text((x + pad[0], y + pad[1]), text, font=fnt, fill=color)
    return h


def foreground(seg):
    """Прозрачный слой поверх фона: бренд, номер новости, заголовок, источник."""
    name, color, _ = seg["theme"]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f_brand = font(FONT_BOLD, 40)
    pill(d, 60, 150, "КРИПТО ЭРА", f_brand, (255, 255, 255, 235), color=(10, 12, 24))
    if seg["kind"] == "news":
        pill(d, W - 60, 150, f"{seg['index']}/{seg['total']}", f_brand, color + (255,), anchor_right=True)
        f_title = font(FONT_BOLD, 60)
        lines = wrap(d, seg["title"], f_title, W - 160, max_lines=6)
        lh = 74
        top = 270
        d.rounded_rectangle((50, top - 30, W - 50, top + lh * len(lines) + 70),
                            radius=36, fill=(8, 10, 22, 185), outline=color + (200,), width=4)
        for i, line in enumerate(lines):
            d.text((80, top + i * lh), line, font=f_title, fill="white")
        if seg.get("source"):
            d.text((80, top + lh * len(lines) + 8), f"Источник: {seg['source']}",
                   font=font(FONT_REG, 34), fill=(200, 205, 220))
    elif seg["kind"] == "intro":
        f_big = font(FONT_BOLD, 96)
        lines = wrap(d, seg["title"], f_big, W - 140)
        for i, line in enumerate(lines):
            d.text((W // 2, 270 + i * 108), line, font=f_big, fill="white", anchor="ma",
                   stroke_width=4, stroke_fill=(0, 0, 0))
        f_date = font(FONT_BOLD, 50)
        label = f"за {seg['date']}"
        tw = d.textlength(label, font=f_date)
        pill(d, W // 2 - (tw + 52) / 2, 270 + len(lines) * 108 + 20, label, f_date,
             color + (255,), color=(10, 12, 24))
    else:  # outro
        f_big = font(FONT_BOLD, 104)
        d.text((W // 2, 270), "БОЛЬШЕ", font=f_big, fill="white", anchor="ma", stroke_width=4, stroke_fill="black")
        d.text((W // 2, 390), "НОВОСТЕЙ", font=f_big, fill="white", anchor="ma", stroke_width=4, stroke_fill="black")
        channel = os.environ.get("TELEGRAM_CHANNEL", "")
        label = f"Telegram {channel}" if channel.startswith("@") else "в нашем Telegram"
        f_mid = font(FONT_BOLD, 54)
        tw = d.textlength(label, font=f_mid)
        pill(d, W // 2 - (tw + 60) / 2, 530, label, f_mid, color + (255,), pad=(30, 16))
        d.text((W // 2, 655), "ссылка в профиле", font=font(FONT_REG, 44), fill=(220, 225, 235), anchor="ma")
    return img


def subtitle_chunks(text, words, duration):
    """Делим озвучку на фразы по 2–4 слова с таймингом."""
    tokens = text.split()
    chunks, cur = [], []
    for tok in tokens:
        cur.append(tok)
        if len(cur) >= 4 or (len(cur) >= 2 and re.search(r"[.,!?:;—]$", tok)) or len(" ".join(cur)) > 22:
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    # тайминг: по словам из edge-tts, иначе пропорционально длине
    timed = []
    if words and len(words) >= len(tokens) * 0.6:
        starts = [w[0] for w in words]
        idx = 0
        ratio = len(words) / len(tokens)
        for ch in chunks:
            n = len(ch.split())
            a = starts[min(int(idx * ratio), len(starts) - 1)]
            timed.append([a, ch])
            idx += n
    else:
        total = sum(len(c) + 3 for c in chunks)
        t = 0.0
        for ch in chunks:
            timed.append([t, ch])
            t += duration * (len(ch) + 3) / total
    result = []
    for i, (a, ch) in enumerate(timed):
        b = timed[i + 1][0] if i + 1 < len(timed) else duration
        result.append((a, max(b, a + 0.2), ch))
    return result


def subtitle_png(text, path):
    img = Image.new("RGBA", (W, 330), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(FONT_BOLD, 78)
    text = re.sub(r"[«»\"]", "", text).strip(" ,;:—-")
    lines = wrap(d, text.upper(), f, 820, max_lines=3)
    lh = 94
    y0 = (330 - lh * len(lines)) // 2
    for i, line in enumerate(lines):
        d.text((W // 2 - 20, y0 + i * lh), line, font=f, fill=(255, 221, 51), anchor="ma",
               stroke_width=7, stroke_fill=(0, 0, 0))
    img.save(path)


# ---------- 5–6. Сборка ----------

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise RuntimeError("ffmpeg error")


def render_segment(seg, i, work):
    text = for_voice(seg["voice"])
    audio, words = tts(text, os.path.join(work, f"a{i}.mp3"))
    if audio:
        speech = ffprobe_duration(audio)
    else:
        speech = max(3.0, len(text) / 15)
    dur = speech + 0.45
    seed = int(hashlib.md5(seg["title"].encode()).hexdigest()[:8], 16)
    bg = os.path.join(work, f"bg{i}.png")
    fg = os.path.join(work, f"fg{i}.png")
    background(seg, seed).save(bg)
    foreground(seg).save(fg)
    subs = subtitle_chunks(seg["voice"], words, speech)
    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.2f}", "-i", bg, "-i", fg]
    for k, (_, _, ch) in enumerate(subs):
        p = os.path.join(work, f"s{i}_{k}.png")
        subtitle_png(ch, p)
        inputs += ["-i", p]
    if audio:
        inputs += ["-i", audio]
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
    a_idx = 2 + len(subs)
    frames = int(dur * FPS)
    zoom_dir = 1 if i % 2 == 0 else -1
    zexpr = f"1+0.08*on/{frames}" if zoom_dir > 0 else f"1.08-0.08*on/{frames}"
    fc = [f"[0:v]scale={W * 2}:{H * 2},zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d=1:s={W}x{H}:fps={FPS}[bg]",
          "[bg][1:v]overlay=0:0[v0]"]
    last = "v0"
    for k, (a, b, _) in enumerate(subs):
        fc.append(f"[{last}][{k + 2}:v]overlay=0:1250:enable='between(t,{a:.2f},{b:.2f})'[v{k + 1}]")
        last = f"v{k + 1}"
    fc.append(f"[{last}]fade=t=in:st=0:d=0.25,format=yuv420p[vout]")
    fc.append(f"[{a_idx}:a]aresample=44100,apad,atrim=0:{dur:.2f}[aout]")
    out = os.path.join(work, f"seg{i}.mp4")
    run(["ffmpeg", "-y", "-hide_banner", *inputs, "-filter_complex", ";".join(fc),
         "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.2f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", out])
    return out, bool(audio)


def concat(parts, out):
    lst = out + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-movflags", "+faststart", out])
    os.remove(lst)


# ---------- 7. Отправка ----------

def send_video(path, caption):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_VIDEO_CHAT")
    if not token or not chat:
        print("TELEGRAM_VIDEO_CHAT не задан: видео только в артефактах.")
        return
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = b""
    fields = {"chat_id": chat, "caption": caption[:1024], "supports_streaming": "true",
              "width": str(W), "height": str(H)}
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{os.path.basename(path)}\"\r\n"
             "Content-Type: video/mp4\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendVideo", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=180)
        print("Видео отправлено в Telegram.")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")


def main():
    news = gather_news()
    if not news:
        print("Нет новостей для видео.")
        return
    date = datetime.now(MSK)
    segments = build_script(news, date)
    os.makedirs(OUT_DIR, exist_ok=True)
    work = os.path.join(OUT_DIR, "work")
    os.makedirs(work, exist_ok=True)
    parts, voiced = [], True
    for i, seg in enumerate(segments):
        print(f"Сегмент {i + 1}/{len(segments)}: {seg['title'][:60]}")
        part, has_voice = render_segment(seg, i, work)
        parts.append(part)
        voiced = voiced and has_voice
    out = os.path.join(OUT_DIR, f"kripto_era_{date:%Y%m%d_%H%M}.mp4")
    concat(parts, out)
    caption = build_caption(news, date)
    with open(os.path.join(OUT_DIR, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(caption)
    if not os.environ.get("VIDEO_KEEP_WORK"):
        shutil.rmtree(work, ignore_errors=True)
    print(f"Готово: {out}, {ffprobe_duration(out):.1f} с, озвучка: {'да' if voiced else 'НЕТ'}")
    print(caption)
    if os.environ.get("VIDEO_SEND", "1") == "1":
        send_video(out, caption)


if __name__ == "__main__":
    main()
    sys.exit(0)
