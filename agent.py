"""
OCTALLION AGENT — Autonomous News + Prediction Market Monitor

On Ritual Chain this maps to:
  - News fetch      → HTTP precompile   0x0801
  - Market fetch    → HTTP precompile   0x0801
  - AI correlation  → LLM precompile    0x0802
  - Autonomous loop → Persistent Agent  0x0820

Ritual Chain:  chain ID 1979 | rpc https://rpc.ritualfoundation.org
RitualWallet:  0x532F0dF0896F353d8C3DD8cc134e8129DA2a3948
Wallet:        0x1bd1418b12073cad0eef94c1cc3dbc1f29adb948
"""

import asyncio
import httpx
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
TELEGRAM_TOKEN  = os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("AGENT_CHAT_ID", "")
AGENT_INTERVAL  = int(os.getenv("AGENT_INTERVAL", "3600"))
WALLET          = "0x1bd1418b12073cad0eef94c1cc3dbc1f29adb948"

NEWS_FEEDS = [
    ("BBC World",   "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Reuters",     "https://feeds.reuters.com/reuters/topNews"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",     "https://decrypt.co/feed"),
]


def fmt_vol(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:.0f}"


async def fetch_news(limit: int = 20) -> list[dict]:
    """Pull headlines from RSS feeds."""
    headlines: list[dict] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for source, url in NEWS_FEEDS:
            try:
                r = await client.get(url)
                r.raise_for_status()
                root = ET.fromstring(r.text)
                # RSS <item> elements
                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    if title:
                        headlines.append({"source": source, "title": title})
                    if len(headlines) >= limit:
                        break
            except Exception as exc:
                print(f"[news] {source}: {exc}")
            if len(headlines) >= limit:
                break
    return headlines[:limit]


async def fetch_markets(limit: int = 40) -> list[dict]:
    """Pull active Polymarket prediction markets sorted by volume."""
    url = (
        f"{POLYMARKET_GAMMA}/events"
        f"?active=true&closed=false&limit={limit}&order=volume&ascending=false"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list):
                events = events.get("data", events.get("events", []))
            markets: list[dict] = []
            for ev in events:
                for m in ev.get("markets") or [ev]:
                    title = m.get("question") or ev.get("title") or ""
                    if not title:
                        continue
                    prices: list = []
                    try:
                        prices = json.loads(m.get("outcomePrices", "[]"))
                    except Exception:
                        pass
                    yes = round(float(prices[0]) * 100) if prices else 50
                    vol = float(m.get("volume") or ev.get("volume") or 0)
                    markets.append({
                        "title": title,
                        "yes":   yes,
                        "no":    100 - yes,
                        "volume": vol,
                        "slug":  m.get("market_slug") or ev.get("slug", ""),
                        "tags":  [t.get("label", "") for t in (ev.get("tags") or [])],
                    })
            return markets
    except Exception as exc:
        print(f"[markets] {exc}")
        return []


async def ai_correlate(headlines: list[dict], markets: list[dict]) -> str:
    """Send news + markets to Claude and return a trade-signal analysis."""
    if not ANTHROPIC_KEY:
        return "Set ANTHROPIC_API_KEY to enable AI correlation."

    news_block = "\n".join(
        f"- [{h['source']}] {h['title']}" for h in headlines
    )
    market_block = "\n".join(
        f"- {m['title']} (YES {m['yes']}¢ | Vol ${fmt_vol(m['volume'])})"
        for m in markets[:25]
    )

    prompt = (
        "You are an autonomous prediction market intelligence agent.\n\n"
        f"BREAKING NEWS:\n{news_block}\n\n"
        f"ACTIVE PREDICTION MARKETS:\n{market_block}\n\n"
        "Instructions:\n"
        "1. Pick 3-5 news stories most likely to shift market prices today.\n"
        "2. For each, name the relevant market and the directional impact (YES↑/↓).\n"
        "3. Flag any market that looks mispriced given today's headlines.\n"
        "4. End with a single line: TRADE SIGNAL: <actionable summary>\n\n"
        "Be concise, specific, and numerical. No disclaimers."
    )

    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 900,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ANTHROPIC_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as exc:
        return f"AI analysis unavailable: {exc}"


async def push_telegram(text: str) -> None:
    """Forward the report to a Telegram chat (optional)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(url, json={
                "chat_id":    TELEGRAM_CHAT,
                "text":       text,
                "parse_mode": "Markdown",
            })
    except Exception as exc:
        print(f"[telegram] {exc}")


async def run_cycle() -> None:
    """One agent cycle: fetch → correlate → report → push."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[{now}] Cycle start")

    headlines, markets = await asyncio.gather(fetch_news(), fetch_markets())
    print(f"[agent] {len(headlines)} headlines | {len(markets)} markets")

    if not headlines and not markets:
        print("[agent] Nothing fetched — skipping")
        return

    analysis = await ai_correlate(headlines, markets)

    sep = "─" * 56
    print(f"\n{sep}")
    print(f"  OCTALLION AGENT — {now}")
    print(f"  Wallet: {WALLET}")
    print(sep)

    print(f"\n  NEWS  ({len(headlines)} headlines)")
    for h in headlines[:12]:
        print(f"    [{h['source']}] {h['title'][:80]}")

    print(f"\n  MARKETS  ({len(markets)} active, top 12 by volume)")
    for m in markets[:12]:
        bar_filled = round(m["yes"] / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        print(f"    YES {m['yes']:>3}¢ {bar} NO {m['no']:>3}¢  "
              f"${fmt_vol(m['volume']):<6}  {m['title'][:50]}")

    print(f"\n  AI ANALYSIS\n")
    for line in analysis.splitlines():
        print(f"    {line}")
    print(f"\n{sep}\n")

    tg_msg = (
        f"⬛ *AGENT REPORT* — _{now}_\n"
        f"{'─' * 30}\n\n"
        f"{analysis[:1000]}\n\n"
        f"📊 `{len(markets)}` markets scanned | `{len(headlines)}` headlines processed\n"
        f"`{WALLET[:10]}…`"
    )
    await push_telegram(tg_msg)


async def main() -> None:
    print("OCTALLION AGENT — autonomous news + prediction market monitor")
    print(f"Wallet:   {WALLET}")
    print(f"Interval: {AGENT_INTERVAL}s")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            await run_cycle()
        except Exception as exc:
            print(f"[agent] cycle error: {exc}")
        await asyncio.sleep(AGENT_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
