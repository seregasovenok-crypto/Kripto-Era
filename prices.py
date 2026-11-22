"""Крипто эра: утренняя и вечерняя сводка курсов (CoinGecko + индекс страха и жадности)."""
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]

COINS = [
    ("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"), ("binancecoin", "BNB"),
    ("ripple", "XRP"), ("the-open-network", "TON"), ("dogecoin", "DOGE"),
]
UA = "Mozilla/5.0 (compatible; KriptoEraBot/1.0)"
MSK = timezone(timedelta(hours=3))


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fmt_price(p):
    if p >= 1000:
        return f"${p:,.0f}".replace(",", " ")
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.4f}"


def fear_greed():
    try:
        d = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        names = {"Extreme Fear": "крайний страх", "Fear": "страх", "Neutral": "нейтрально",
                 "Greed": "жадность", "Extreme Greed": "крайняя жадность"}
        return f"{d['value']}/100 ({names.get(d['value_classification'], d['value_classification'])})"
    except Exception as e:  # noqa: BLE001
        print("Индекс страха недоступен:", e)
        return None


def build():
    ids = ",".join(c for c, _ in COINS)
    data = get_json(
        "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&price_change_percentage=24h&ids=" + ids
    )
    by_id = {d["id"]: d for d in data}
    now = datetime.now(MSK)
    title = "☀️ Утренняя сводка" if now.hour < 15 else "🌙 Итоги дня"
    lines = [f"<b>{title} · {now:%d.%m}</b>", ""]
    for cid, sym in COINS:
        d = by_id.get(cid)
        if not d or d.get("current_price") is None:
            continue
        ch = d.get("price_change_percentage_24h") or 0.0
        arrow = "🟢" if ch >= 0 else "🔴"
        lines.append(f"{arrow} <b>{sym}</b> {fmt_price(d['current_price'])}  ({ch:+.1f}%)")
    fg = fear_greed()
    if fg:
        lines += ["", f"😱 Индекс страха и жадности: {html.escape(fg)}"]
    lines += ["", "Изменение за 24 часа · #курсы"]
    return "\n".join(lines)


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHANNEL, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=30)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")


if __name__ == "__main__":
    text = build()
    print(text)
    send(text)
    sys.exit(0)
