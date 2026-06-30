"""
OCTALLION — Polymarket Trading Bot for Telegram
"""

import asyncio
import os
import json
import logging
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY_HERE")
ALERT_THRESHOLD  = float(os.getenv("ALERT_THRESHOLD", "5"))
DAILY_HOUR       = int(os.getenv("DAILY_HOUR", "8"))
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"
AGENT_INTERVAL   = int(os.getenv("AGENT_INTERVAL", "3600"))
WALLET           = "0x1bd1418b12073cad0eef94c1cc3dbc1f29adb948"

NEWS_FEEDS = [
    ("BBC World",     "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Reuters",       "https://feeds.reuters.com/reuters/topNews"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
]

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("octallion")

STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "watchlist": {}, "subscribers": [], "price_cache": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

state = load_state()

async def fetch_markets(tag="", limit=20):
    url = f"{POLYMARKET_GAMMA}/events?active=true&closed=false&limit={limit}&order=volume&ascending=false"
    if tag:
        url += f"&tag_slug={tag}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list):
                events = events.get("data", events.get("events", []))
            markets = []
            for ev in events:
                for m in (ev.get("markets") or [ev]):
                    title = m.get("question") or ev.get("title") or ev.get("slug", "")
                    if not title:
                        continue
                    prices = []
                    try:
                        prices = json.loads(m.get("outcomePrices", "[]"))
                    except Exception:
                        pass
                    yes_price = round(float(prices[0]) * 100) if prices else 50
                    vol = float(m.get("volume") or ev.get("volume") or 0)
                    markets.append({
                        "id": m.get("id") or ev.get("id"),
                        "title": title,
                        "yes": yes_price,
                        "no": 100 - yes_price,
                        "volume": vol,
                        "slug": m.get("market_slug") or ev.get("slug", ""),
                        "tags": [t.get("label", t.get("slug", "")) for t in (ev.get("tags") or [])],
                    })
            return markets
    except Exception as e:
        log.error(f"fetch_markets error: {e}")
        return []

async def fetch_market_by_keyword(keyword):
    all_markets = await fetch_markets(limit=50)
    return [m for m in all_markets if keyword.lower() in m["title"].lower()]

async def ai_analyze(market, user_prob=None):
    edge_line = ""
    if user_prob is not None:
        edge = user_prob - market["yes"]
        edge_line = f"\nUser estimate: {user_prob}%\nEdge: {edge:+d}pp"
    prompt = f"""Analyze this Polymarket market:
Market: {market['title']}
YES: {market['yes']}c | NO: {market['no']}c
Volume: ${fmt_vol(market['volume'])}{edge_line}

4-paragraph breakdown:
1. Key factors driving the probability
2. Where the crowd might be wrong
3. Historical base rates
4. Trade recommendation with sizing and one key risk

Be direct. Numbers only. No disclaimers."""
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 800,
        "system": "You are a sharp Polymarket analyst specialising in NBA, soccer, and crypto. Be punchy and specific.",
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ANTHROPIC_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as e:
        return f"Analysis unavailable: {e}"

async def ai_chat(user_message):
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 600,
        "system": "You are Octallion, a sharp Polymarket trading assistant. NBA, soccer, crypto specialist. Be concise and actionable — this is Telegram.",
        "messages": [{"role": "user", "content": user_message}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ANTHROPIC_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as e:
        return f"AI unavailable: {e}"

def fmt_vol(v):
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000: return f"{v/1_000:.0f}K"
    return f"{v:.0f}"

def bar(pct, width=10):
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def market_card(m, idx=None):
    prefix = f"`{idx}.` " if idx is not None else ""
    emoji = "🟢" if m["yes"] < 40 else "🔴" if m["yes"] > 70 else "🟡"
    return (f"{prefix}*{m['title']}*\n"
            f"`YES {m['yes']:>3}c {bar(m['yes'])} NO {m['no']:>3}c`\n"
            f"Vol: `${fmt_vol(m['volume'])}` {emoji}\n")

async def job_price_alerts(context):
    if not state["watchlist"]:
        return
    try:
        markets = await fetch_markets(limit=50)
        market_map = {m["title"].lower(): m for m in markets}
        cache = state.get("price_cache", {})
        for cid, keywords in state["watchlist"].items():
            for kw in keywords:
                for title_lc, m in market_map.items():
                    if kw.lower() in title_lc:
                        prev = cache.get(m["id"])
                        curr = m["yes"]
                        if prev is not None and abs(curr - prev) >= ALERT_THRESHOLD:
                            direction = "🔺" if curr > prev else "🔻"
                            try:
                                await context.bot.send_message(
                                    chat_id=int(cid),
                                    text=(f"{direction} *PRICE ALERT*\n_{m['title'][:50]}_\n\n"
                                          f"`{prev}c → {curr}c ({curr-prev:+.0f}c)`\n"
                                          f"Vol: `${fmt_vol(m['volume'])}`"),
                                    parse_mode=ParseMode.MARKDOWN)
                            except Exception:
                                pass
                        cache[m["id"]] = curr
        state["price_cache"] = cache
        save_state(state)
    except Exception as e:
        log.error(f"price alert error: {e}")

async def job_daily_summary(context):
    if not state["subscribers"]:
        return
    try:
        markets = await fetch_markets(limit=20)
        nba = [m for m in markets if any("nba" in t.lower() for t in m["tags"])][:3]
        soccer = [m for m in markets if any("soccer" in t.lower() for t in m["tags"])][:3]
        top = sorted(markets, key=lambda m: m["volume"], reverse=True)[:3]
        now = datetime.utcnow().strftime("%b %d, %Y")
        text = f"⬛ *OCTALLION — {now}*\n" + "─"*28 + "\n\n"
        if nba:
            text += "🏀 *NBA*\n"
            for m in nba:
                text += f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n"
            text += "\n"
        if soccer:
            text += "⚽ *SOCCER*\n"
            for m in soccer:
                text += f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n"
            text += "\n"
        if top:
            text += "🔥 *TOP VOLUME*\n"
            for m in top:
                text += f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n"
        for cid in state["subscribers"]:
            try:
                await context.bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
    except Exception as e:
        log.error(f"daily summary error: {e}")

async def cmd_start(update, ctx):
    cid = update.effective_chat.id
    if cid not in state["subscribers"]:
        state["subscribers"].append(cid)
        save_state(state)
    await update.message.reply_text(
        "⬛ *OCTALLION* — Polymarket Trading Bot\n\n"
        "`/markets` — live top markets\n"
        "`/nba` — NBA markets\n"
        "`/soccer` — soccer markets\n"
        "`/analyze <keyword>` — AI analysis\n"
        "`/news` — agent scan: news → market signals\n"
        "`/add <market> <YES/NO> <entry> <amount>` — track position\n"
        "`/positions` — your positions + P&L\n"
        "`/update <#> <new price>` — update position\n"
        "`/remove <#>` — remove position\n"
        "`/watch <keyword>` — price alerts\n"
        "`/watchlist` — view watchlist\n"
        "`/summary` — market summary\n\n"
        "Or just *ask me anything* 🎯",
        parse_mode=ParseMode.MARKDOWN)

async def cmd_markets(update, ctx):
    await update.message.reply_text("⬛ Fetching live markets...")
    markets = await fetch_markets(limit=10)
    if not markets:
        await update.message.reply_text("No markets found. Try again.")
        return
    text = "⬛ *TOP MARKETS*\n" + "─"*28 + "\n\n"
    for i, m in enumerate(markets[:8], 1):
        text += market_card(m, i) + "\n"
    text += "_Use /analyze keyword for deep analysis_"
    ctx.bot_data["last_markets"] = {m["id"]: m for m in markets}
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_nba(update, ctx):
    await update.message.reply_text("🏀 Fetching NBA markets...")
    markets = await fetch_markets(tag="nba", limit=10)
    if not markets:
        all_m = await fetch_markets(limit=50)
        markets = [m for m in all_m if any("nba" in t.lower() for t in m["tags"])]
    if not markets:
        await update.message.reply_text("No NBA markets found right now.")
        return
    text = "🏀 *NBA MARKETS*\n" + "─"*28 + "\n\n"
    for i, m in enumerate(markets[:8], 1):
        text += market_card(m, i) + "\n"
    ctx.bot_data["last_markets"] = {m["id"]: m for m in markets}
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_soccer(update, ctx):
    await update.message.reply_text("⚽ Fetching soccer markets...")
    markets = await fetch_markets(tag="soccer", limit=10)
    if not markets:
        all_m = await fetch_markets(limit=50)
        markets = [m for m in all_m if any("soccer" in t.lower() or "football" in t.lower() for t in m["tags"])]
    if not markets:
        await update.message.reply_text("No soccer markets found.")
        return
    text = "⚽ *SOCCER MARKETS*\n" + "─"*28 + "\n\n"
    for i, m in enumerate(markets[:8], 1):
        text += market_card(m, i) + "\n"
    ctx.bot_data["last_markets"] = {m["id"]: m for m in markets}
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_analyze(update, ctx):
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Usage: `/analyze lakers`", parse_mode=ParseMode.MARKDOWN)
        return
    msg = await update.message.reply_text(f"🔍 Searching: _{query}_...", parse_mode=ParseMode.MARKDOWN)
    markets = await fetch_market_by_keyword(query)
    if not markets:
        await msg.edit_text("No markets found. Try different keywords.")
        return
    m = markets[0]
    await msg.edit_text(f"🤖 Analyzing: _{m['title']}_...", parse_mode=ParseMode.MARKDOWN)
    analysis = await ai_analyze(m)
    text = (f"⬛ *OCTALLION ANALYSIS*\n─────────────────────\n"
            f"*{m['title']}*\n\n"
            f"`YES {m['yes']}c {bar(m['yes'])} NO {m['no']}c`\n"
            f"Vol: `${fmt_vol(m['volume'])}`\n\n"
            f"─────────────────────\n{analysis}")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📌 Track", callback_data=f"track_{m['id']}"),
        InlineKeyboardButton("👀 Watch", callback_data=f"watch_{m['id']}"),
    ]])
    ctx.bot_data[f"market_{m['id']}"] = m
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def cmd_add(update, ctx):
    args = ctx.args
    if not args or len(args) < 4:
        await update.message.reply_text(
            "Usage: `/add <market name> <YES or NO> <entry cents> <dollar amount>`\n"
            "Example: `/add lakers YES 35 50`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = float(args[-1])
        entry  = float(args[-2])
        side   = args[-3].upper()
        name   = " ".join(args[:-3])
        if side not in ("YES", "NO"):
            raise ValueError("Side must be YES or NO")
        cid = str(update.effective_chat.id)
        if cid not in state["positions"]:
            state["positions"][cid] = []
        state["positions"][cid].append({
            "name": name, "side": side, "entry": entry,
            "current": entry, "amount": amount,
            "added": datetime.utcnow().isoformat(),
        })
        save_state(state)
        shares = amount / (entry / 100)
        await update.message.reply_text(
            f"✅ *Position added*\n*{name}*\n"
            f"`{side} @ {entry}c` | `${amount:.0f}` | `{shares:.1f} shares`",
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\nExample: `/add lakers YES 35 50`",
                                        parse_mode=ParseMode.MARKDOWN)

async def cmd_positions(update, ctx):
    cid = str(update.effective_chat.id)
    pos_list = state["positions"].get(cid, [])
    if not pos_list:
        await update.message.reply_text("No positions yet. Use `/add` to track one.", parse_mode=ParseMode.MARKDOWN)
        return
    total_in = sum(p["amount"] for p in pos_list)
    total_pnl = 0.0
    text = "⬛ *YOUR POSITIONS*\n" + "─"*28 + "\n\n"
    for i, p in enumerate(pos_list, 1):
        entry = p["entry"]; current = p.get("current", entry); amount = p["amount"]
        shares = amount / (entry / 100)
        cur_val = shares * (current / 100)
        pnl = cur_val - amount
        total_pnl += pnl
        pct = (pnl / amount * 100) if amount else 0
        arrow = "📈" if pnl > 0 else "📉" if pnl < 0 else "➡️"
        text += (f"{i}. {arrow} *{p['name'][:35]}*\n"
                 f"`{p['side']} @ {entry}c` → `{current}c`\n"
                 f"In: `${amount:.0f}` | P&L: `{'+'if pnl>=0 else ''}{pnl:.2f} ({pct:+.1f}%)`\n\n")
    text += "─"*28 + f"\n*Invested:* `${total_in:.2f}` | *P&L:* `{'+'if total_pnl>=0 else ''}{total_pnl:.2f}`"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_update(update, ctx):
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Usage: `/update <#> <new price>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        idx = int(ctx.args[0]) - 1
        new_p = float(ctx.args[1])
        cid = str(update.effective_chat.id)
        pos_list = state["positions"].get(cid, [])
        if idx < 0 or idx >= len(pos_list):
            await update.message.reply_text("Position not found.")
            return
        old_p = pos_list[idx].get("current", pos_list[idx]["entry"])
        pos_list[idx]["current"] = new_p
        save_state(state)
        move = new_p - old_p
        arrow = "📈" if move > 0 else "📉" if move < 0 else "➡️"
        await update.message.reply_text(f"{arrow} Position #{idx+1} updated to `{new_p}c` ({move:+.1f}c)",
                                        parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_remove(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: `/remove <#>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        idx = int(ctx.args[0]) - 1
        cid = str(update.effective_chat.id)
        plist = state["positions"].get(cid, [])
        if idx < 0 or idx >= len(plist):
            await update.message.reply_text("Position not found.")
            return
        removed = plist.pop(idx)
        save_state(state)
        await update.message.reply_text(f"🗑 Removed: _{removed['name']}_", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_watch(update, ctx):
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Usage: `/watch celtics`", parse_mode=ParseMode.MARKDOWN)
        return
    cid = str(update.effective_chat.id)
    if cid not in state["watchlist"]:
        state["watchlist"][cid] = []
    if query not in state["watchlist"][cid]:
        state["watchlist"][cid].append(query)
        save_state(state)
    await update.message.reply_text(f"👀 Watching: _{query}_", parse_mode=ParseMode.MARKDOWN)

async def cmd_watchlist(update, ctx):
    cid = str(update.effective_chat.id)
    wl = state["watchlist"].get(cid, [])
    if not wl:
        await update.message.reply_text("No watchlist. Use `/watch <keyword>`.", parse_mode=ParseMode.MARKDOWN)
        return
    text = "👀 *WATCHLIST*\n\n" + "\n".join(f"{i}. _{w}_" for i, w in enumerate(wl, 1))
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_summary(update, ctx):
    msg = await update.message.reply_text("⬛ Generating summary...")
    markets = await fetch_markets(limit=20)
    nba = [m for m in markets if any("nba" in t.lower() for t in m["tags"])][:3]
    soccer = [m for m in markets if any("soccer" in t.lower() for t in m["tags"])][:3]
    top = sorted(markets, key=lambda m: m["volume"], reverse=True)[:3]
    now = datetime.utcnow().strftime("%b %d, %Y %H:%M UTC")
    text = f"⬛ *OCTALLION SUMMARY*\n_{now}_\n" + "─"*28 + "\n\n"
    if nba:
        text += "🏀 *NBA*\n" + "".join(f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n" for m in nba) + "\n"
    if soccer:
        text += "⚽ *SOCCER*\n" + "".join(f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n" for m in soccer) + "\n"
    if top:
        text += "🔥 *TOP VOLUME*\n" + "".join(f"• {m['title'][:45]}\n  `YES {m['yes']}c` | `${fmt_vol(m['volume'])}`\n" for m in top)
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)

async def fetch_headlines(limit=15):
    headlines = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for source, url in NEWS_FEEDS:
            try:
                r = await client.get(url)
                r.raise_for_status()
                root = ET.fromstring(r.text)
                for item in root.findall(".//item"):
                    title = (item.findtext("title") or "").strip()
                    if title:
                        headlines.append({"source": source, "title": title})
                    if len(headlines) >= limit:
                        break
            except Exception as e:
                log.warning(f"news feed {source}: {e}")
            if len(headlines) >= limit:
                break
    return headlines[:limit]


async def ai_news_correlate(headlines, markets):
    if not ANTHROPIC_KEY or ANTHROPIC_KEY == "YOUR_ANTHROPIC_KEY_HERE":
        return "Set ANTHROPIC_API_KEY to enable AI correlation."
    news_block = "\n".join(f"- [{h['source']}] {h['title']}" for h in headlines)
    market_block = "\n".join(
        f"- {m['title']} (YES {m['yes']}c | Vol ${fmt_vol(m['volume'])})"
        for m in markets[:20]
    )
    prompt = (
        "You are an autonomous prediction market intelligence agent.\n\n"
        f"BREAKING NEWS:\n{news_block}\n\n"
        f"ACTIVE PREDICTION MARKETS:\n{market_block}\n\n"
        "1. Pick 3-5 news stories most likely to shift market prices today.\n"
        "2. For each, name the relevant market and directional impact (YES↑/↓).\n"
        "3. Flag any market that looks mispriced given today's news.\n"
        "4. End with: TRADE SIGNAL: <one-line summary>\n\n"
        "Be concise, specific, numerical. No disclaimers."
    )
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
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
    except Exception as e:
        return f"AI unavailable: {e}"


async def cmd_news(update, ctx):
    msg = await update.message.reply_text("📰 Scanning news + markets...")
    headlines, markets = await asyncio.gather(fetch_headlines(), fetch_markets(limit=30))
    if not headlines:
        await msg.edit_text("Could not fetch news feeds right now.")
        return
    analysis = await ai_news_correlate(headlines, markets)
    now = datetime.utcnow().strftime("%b %d %H:%M UTC")
    text = (
        f"⬛ *AGENT REPORT* — _{now}_\n"
        f"{'─' * 30}\n\n"
        f"📰 *LATEST HEADLINES*\n"
        + "".join(f"• [{h['source']}] {h['title'][:60]}\n" for h in headlines[:8])
        + f"\n{'─' * 30}\n\n"
        f"🤖 *AI SIGNAL*\n{analysis[:900]}"
    )
    await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)


async def job_agent_cycle(context):
    """Periodic autonomous news + market scan pushed to AGENT_CHAT_ID."""
    agent_chat = os.getenv("AGENT_CHAT_ID", "")
    if not agent_chat:
        return
    try:
        headlines, markets = await asyncio.gather(fetch_headlines(), fetch_markets(limit=30))
        analysis = await ai_news_correlate(headlines, markets)
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        text = (
            f"⬛ *AGENT CYCLE* — _{now}_\n"
            f"{'─' * 30}\n\n"
            f"{analysis[:1000]}\n\n"
            f"📊 `{len(markets)}` markets | `{len(headlines)}` headlines\n"
            f"`{WALLET[:14]}…`"
        )
        await context.bot.send_message(chat_id=int(agent_chat), text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error(f"agent cycle: {e}")


async def handle_message(update, ctx):
    text = update.message.text.strip()
    if not text:
        return
    msg = await update.message.reply_text("🤖 Thinking...")
    reply = await ai_chat(text)
    await msg.edit_text(reply, parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update, ctx):
    q = update.callback_query
    data = q.data
    await q.answer()
    if data.startswith("analyze_"):
        mid = data.split("_", 1)[1]
        m = ctx.bot_data.get("last_markets", {}).get(mid)
        if not m:
            await q.message.reply_text("Market expired. Use `/analyze keyword`.", parse_mode=ParseMode.MARKDOWN)
            return
        await q.message.reply_text("🤖 Analyzing...")
        analysis = await ai_analyze(m)
        text = (f"⬛ *ANALYSIS*\n─────────────────────\n*{m['title']}*\n\n"
                f"`YES {m['yes']}c {bar(m['yes'])} NO {m['no']}c`\n"
                f"Vol: `${fmt_vol(m['volume'])}`\n\n─────────────────────\n{analysis}")
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("watch_"):
        mid = data.split("_", 1)[1]
        m = ctx.bot_data.get(f"market_{mid}") or ctx.bot_data.get("last_markets", {}).get(mid)
        if m:
            cid = str(update.effective_chat.id)
            if cid not in state["watchlist"]:
                state["watchlist"][cid] = []
            key = m["title"][:50]
            if key not in state["watchlist"][cid]:
                state["watchlist"][cid].append(key)
                save_state(state)
            await q.message.reply_text(f"👀 Watching: _{key}_", parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("track_"):
        mid = data.split("_", 1)[1]
        m = ctx.bot_data.get(f"market_{mid}") or ctx.bot_data.get("last_markets", {}).get(mid)
        if m:
            await q.message.reply_text(
                f"To track this:\n`/add {m['title'][:30]} YES {m['yes']} 50`\n\nChange YES/NO and 50 to your values.",
                parse_mode=ParseMode.MARKDOWN)

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_start))
    app.add_handler(CommandHandler("markets",   cmd_markets))
    app.add_handler(CommandHandler("nba",       cmd_nba))
    app.add_handler(CommandHandler("soccer",    cmd_soccer))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("update",    cmd_update))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("watch",     cmd_watch))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(CommandHandler("news",      cmd_news))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(job_price_alerts, interval=900, first=60)
    app.job_queue.run_daily(job_daily_summary, time=dtime(hour=DAILY_HOUR, minute=0))
    app.job_queue.run_repeating(job_agent_cycle, interval=AGENT_INTERVAL, first=120)

    log.info("OCTALLION online")
    app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
