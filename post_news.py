"""Крипто эра: публикует отобранные крипто-новости в Telegram-канал.

За один запуск публикуется не больше MAX_POSTS_PER_RUN постов, чтобы канал
не превращался в спам (охват постов = цена рекламы).
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import feedparser
from deep_translator import GoogleTranslator, MyMemoryTranslator

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]

# (название, RSS, нужен перевод)
FEEDS = [
    ("ForkLog", "https://forklog.com/feed", False),
    ("Bits.media", "https://bits.media/rss2/", False),
    ("BeInCrypto", "https://ru.beincrypto.com/feed/", False),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", True),
    ("Cointelegraph", "https://cointelegraph.com/rss", True),
    ("Decrypt", "https://decrypt.co/feed", True),
]

STATE_FILE = "state.json"
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MAX_AGE_HOURS = 12          # не публикуем старьё
SUMMARY_LEN = 300
FEED_TIMEOUT = 20
TRANSLATE_TIMEOUT = 20
SIMILARITY_LIMIT = 0.5      # похожие заголовки считаем дублями
UA = "Mozilla/5.0 (compatible; KriptoEraBot/1.0)"

HASHTAGS = [
    (r"bitcoin|биткоин|биткойн|\bbtc\b", "#BTC"),
    (r"ethereum|эфир|\beth\b", "#ETH"),
    (r"solana|солан|\bsol\b", "#SOL"),
    (r"\bton\b|toncoin|тонкоин|telegram", "#TON"),
    (r"\bxrp\b|ripple", "#XRP"),
    (r"binance|бинанс|\bbnb\b", "#Binance"),
    (r"\betf\b", "#ETF"),
    (r"\bsec\b|регулир|regulat|закон|law", "#регулирование"),
    (r"hack|взлом|exploit|эксплойт|украл|stolen", "#взлом"),
    (r"stablecoin|стейблкоин|usdt|usdc|tether", "#стейблкоины"),
    (r"\bnft\b", "#NFT"),
    (r"defi", "#DeFi"),
]

POOL = ThreadPoolExecutor(max_workers=4)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("seen", [])
    data.setdefault("recent_titles", [])
    data.setdefault("last_source", "")
    return data


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    state["recent_titles"] = state["recent_titles"][-60:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


def clean(text, limit=None):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # хвосты вида "The post ... appeared first on ..."
    text = re.sub(r"(The post|Сообщение) .*? (appeared first on|появились сначала на).*$", "", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def has_cyrillic(text):
    return bool(re.search(r"[а-яА-ЯёЁ]", text or ""))


def words(text):
    return {w for w in re.findall(r"[a-zа-яё0-9]{4,}", text.lower())}


def is_duplicate(title, recent):
    a = words(title)
    if not a:
        return False
    for other in recent:
        b = words(other)
        if b and len(a & b) / len(a | b) >= SIMILARITY_LIMIT:
            return True
    return False


def translate(text):
    for name, make in (
        ("google", lambda: GoogleTranslator(source="en", target="ru")),
        ("mymemory", lambda: MyMemoryTranslator(source="en-GB", target="ru-RU")),
    ):
        try:
            result = POOL.submit(lambda: make().translate(text)).result(timeout=TRANSLATE_TIMEOUT)
            if result and has_cyrillic(result):
                return result
            print(f"[{name}] пустой или не русский перевод")
        except FutureTimeout:
            print(f"[{name}] таймаут")
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] ошибка: {str(e)[:80]}")
        time.sleep(2)
    return None


def translate_pair(title, summary):
    result = translate(f"{title}\n|||\n{summary}" if summary else title)
    if not result:
        return None
    parts = [p.strip() for p in result.split("|||")]
    return parts[0], (parts[1] if len(parts) > 1 else "")


def fetch_feed(url):
    def _parse():
        return feedparser.parse(url, agent=UA)
    return POOL.submit(_parse).result(timeout=FEED_TIMEOUT)


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else None


def hashtags(text):
    found = []
    for pattern, tag in HASHTAGS:
        if re.search(pattern, text, re.IGNORECASE) and tag not in found:
            found.append(tag)
    return " ".join(found[:3]) or "#крипто"


def build_post(title, summary, source, link):
    esc = html.escape
    tags = hashtags(f"{title} {summary}")
    body = f"<b>{esc(title)}</b>"
    if summary:
        body += f"\n\n{esc(summary)}"
    body += f"\n\n{tags}\n\n<a href=\"{esc(link, quote=True)}\">Источник: {esc(source)}</a>"
    return body


def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=30)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")


def collect(state):
    seen = set(state["seen"])
    first_run = not seen
    now = time.time()
    candidates = []
    for source, url, need_translate in FEEDS:
        try:
            feed = fetch_feed(url)
        except Exception as e:  # noqa: BLE001
            print("Ошибка ленты:", source, e)
            continue
        print(f"{source}: {len(feed.entries)} записей")
        for entry in feed.entries[:15]:
            link = entry.get("link")
            if not link or link in seen:
                continue
            if first_run:
                state["seen"].append(link)
                continue
            ts = entry_time(entry) or now
            if now - ts > MAX_AGE_HOURS * 3600:
                state["seen"].append(link)
                continue
            candidates.append({
                "source": source, "link": link, "ts": ts, "translate": need_translate,
                "title": clean(entry.get("title", "")),
                "summary": clean(entry.get("summary", ""), SUMMARY_LEN),
            })
    if first_run:
        print("Первый запуск: запомнил текущие новости, ничего не публикую.")
    # свежие сначала; источник, который был в прошлый раз, уходит в конец
    candidates.sort(key=lambda c: (c["source"] == state["last_source"], -c["ts"]))
    return candidates


def main():
    state = load_state()
    posted = 0
    for c in collect(state):
        if posted >= MAX_POSTS_PER_RUN:
            break
        title, summary = c["title"], c["summary"]
        if c["translate"]:
            pair = translate_pair(title, summary)
            if pair is None:
                print("Перевод не удался, попробую в следующий раз:", title[:60])
                continue
            title, summary = pair
        if is_duplicate(title, state["recent_titles"]):
            print("Дубль, пропускаю:", title[:60])
            state["seen"].append(c["link"])
            continue
        try:
            send(build_post(title, summary, c["source"], c["link"]))
        except Exception as e:  # noqa: BLE001
            print("Ошибка отправки:", e)
            continue
        state["seen"].append(c["link"])
        state["recent_titles"].append(title)
        state["last_source"] = c["source"]
        posted += 1
        time.sleep(3)
    save_state(state)
    print("Опубликовано:", posted)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
