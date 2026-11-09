#!/usr/bin/env python3
"""
Six-bot paper trading desk: scout, news, chart, skeptic, sizer, executor.

Trades US stocks and crypto on an Alpaca PAPER account. Every new trade is
emailed to you first and only placed after you reply APPROVE.
Protective exits (stop-loss, take-profit, time limit) close positions on
their own and email you what they did.

Commands:
  python desk.py check            test connections (Alpaca, AI, Gmail)
  python desk.py run              one full six-bot cycle (cron: every 30 min)
  python desk.py run --dry-run    same, but prints proposals, sends nothing
  python desk.py approvals        read your replies, place approved orders (cron: every 5 min)
  python desk.py status           account, positions and recent requests
  python desk.py report           score the desk's past calls and check the go-live bar

Settings live in .env next to this file (see the setup guide).
"""

import email
import imaplib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- settings

def load_env(path=os.path.join(HERE, ".env")):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(name, default=None):
    return os.environ.get(name, default)


ALPACA_KEY = env("ALPACA_API_KEY", "")
ALPACA_SECRET = env("ALPACA_SECRET_KEY", "")
TRADE_URL = "https://paper-api.alpaca.markets"   # paper only, by design
DATA_URL = "https://data.alpaca.markets"

AI_PROVIDER = env("AI_PROVIDER", "gemini").lower()      # gemini | claude
GEMINI_KEY = env("GEMINI_API_KEY", "")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK = env("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
ANTHROPIC_KEY = env("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_SKEPTIC_MODEL = env("CLAUDE_SKEPTIC_MODEL", "claude-sonnet-5-5")

DESK_GMAIL = env("DESK_GMAIL", "")
DESK_GMAIL_PW = env("DESK_GMAIL_APP_PASSWORD", "").replace(" ", "")
APPROVAL_EMAIL = env("APPROVAL_EMAIL", "")

STOCKS = [s.strip().upper() for s in env(
    "STOCK_WATCHLIST", "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,SPY,QQQ").split(",") if s.strip()]
CRYPTO = [s.strip().upper() for s in env(
    "CRYPTO_WATCHLIST", "BTC/USD,ETH/USD,SOL/USD").split(",") if s.strip()]

MAX_POSITION_PCT = float(env("MAX_POSITION_PCT", "5"))        # % of equity per trade
MAX_OPEN_POSITIONS = int(env("MAX_OPEN_POSITIONS", "8"))
MAX_NEW_TRADES = int(env("MAX_NEW_TRADES_PER_CYCLE", "2"))
MIN_CONFIDENCE = float(env("MIN_CONFIDENCE", "0.6"))
APPROVAL_MINUTES = int(env("APPROVAL_MINUTES", "30"))
SCOUT_PICKS = int(env("SCOUT_PICKS", "6"))
QUIET_SCORE = float(env("QUIET_SCORE", "1.5"))          # below this, skip the AI bots

AUTO_EXITS = env("AUTO_EXITS", "true").lower() == "true"
STOP_LOSS_STOCK_PCT = float(env("STOP_LOSS_STOCK_PCT", "5"))
STOP_LOSS_CRYPTO_PCT = float(env("STOP_LOSS_CRYPTO_PCT", "8"))
TAKE_PROFIT_PCT = float(env("TAKE_PROFIT_PCT", "10"))
MAX_HOLD_DAYS = float(env("MAX_HOLD_DAYS", "7"))

DB_PATH = os.path.join(HERE, "desk.db")


PUBLIC_LOGS = env("GITHUB_ACTIONS") == "true"   # GitHub logs of public repos are public


def hide(text):
    """In public logs, hide approval codes so nobody can reuse them."""
    return re.sub(r"DESK-[0-9a-f]{6}", "DESK-******", text) if PUBLIC_LOGS else text


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {hide(msg)}", flush=True)


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- storage

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS requests (
        token TEXT PRIMARY KEY, created_at TEXT, symbol TEXT, asset TEXT,
        side TEXT, notional REAL, confidence REAL, reasoning TEXT,
        status TEXT, updated_at TEXT, order_id TEXT, note TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS decisions (
        cycle_at TEXT, symbol TEXT, asset TEXT, price REAL, features TEXT,
        sentiment REAL, news TEXT, chart_action TEXT, chart_conf REAL, chart_reason TEXT,
        skeptic TEXT, outcome TEXT, dry_run INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS trades (
        symbol TEXT, asset TEXT, opened_at TEXT, notional REAL,
        closed_at TEXT, pl REAL, pl_pct REAL, exit_reason TEXT)""")
    return con


def _short(text, n=110):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


# ---------------------------------------------------------------- alpaca

def _alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def http_get(url, params=None, headers=None):
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def http_post(url, body, headers=None):
    r = requests.post(url, json=body, headers=headers, timeout=60)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}", response=r)
    return r.json()


def http_delete(url, headers=None):
    r = requests.delete(url, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}", response=r)
    return r.json() if r.text else {}


def account():
    return http_get(f"{TRADE_URL}/v2/account", headers=_alpaca_headers())


def positions():
    return http_get(f"{TRADE_URL}/v2/positions", headers=_alpaca_headers())


def market_open():
    return bool(http_get(f"{TRADE_URL}/v2/clock", headers=_alpaca_headers()).get("is_open"))


def _paged_bars(url, symbols, start, timeframe="1Hour"):
    out, token = {}, None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": timeframe,
                  "start": start, "limit": 10000}
        if "stocks" in url:
            params["feed"] = "iex"   # free data feed
        if token:
            params["page_token"] = token
        data = http_get(url, params=params, headers=_alpaca_headers())
        for sym, bars in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bars)
        token = data.get("next_page_token")
        if not token:
            return out


def bars(symbols, crypto):
    if not symbols:
        return {}
    start = (now() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{DATA_URL}/v1beta3/crypto/us/bars" if crypto else f"{DATA_URL}/v2/stocks/bars"
    return _paged_bars(url, symbols, start)


def bars_since(symbols, crypto, start, timeframe="1Hour"):
    if not symbols:
        return {}
    url = f"{DATA_URL}/v1beta3/crypto/us/bars" if crypto else f"{DATA_URL}/v2/stocks/bars"
    return _paged_bars(url, symbols, start.strftime("%Y-%m-%dT%H:%M:%SZ"), timeframe)


def news(symbols):
    if not symbols:
        return []
    start = (now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    syms = ",".join(s.replace("/", "") for s in symbols)
    data = http_get(f"{DATA_URL}/v1beta1/news", headers=_alpaca_headers(),
                    params={"symbols": syms, "start": start, "limit": 50, "sort": "desc"})
    return data.get("news", [])


# ---------------------------------------------------------------- AI

def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _gemini(system, prompt, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    data = http_post(url, body, headers={"x-goog-api-key": GEMINI_KEY})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _claude(system, prompt, model):
    body = {"model": model, "max_tokens": 2000, "system": system,
            "messages": [{"role": "user", "content": prompt + "\n\nReply with JSON only."}]}
    data = http_post("https://api.anthropic.com/v1/messages", body,
                     headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in data["content"])


def ask_ai(system, prompt, heavy=False):
    """Ask the AI for JSON. heavy=True is used for the skeptic."""
    if AI_PROVIDER == "claude":
        return _parse_json(_claude(system, prompt, CLAUDE_SKEPTIC_MODEL if heavy else CLAUDE_MODEL))
    last = None
    for model in (GEMINI_MODEL, GEMINI_FALLBACK):
        for attempt in range(2):
            try:
                return _parse_json(_gemini(system, prompt, model))
            except requests.HTTPError as e:
                last = e
                code = getattr(e.response, "status_code", 0)
                if code == 429 and attempt == 0:
                    time.sleep(20)          # free-tier rate limit: wait and retry once
                    continue
                break                       # try the fallback model
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                last = e
                break
    raise RuntimeError(f"AI request failed: {last}")


# ---------------------------------------------------------------- indicators

def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(vals, n=14):
    if len(vals) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def features(bar_list):
    closes = [b["c"] for b in bar_list]
    vols = [b["v"] for b in bar_list]
    if len(closes) < 25:
        return None
    last = closes[-1]

    def chg(h):
        return round((last / closes[-h - 1] - 1) * 100, 2) if len(closes) > h else None

    avg_vol = sum(vols[-25:-1]) / 24 or 1
    return {"price": round(last, 4), "chg_1h_pct": chg(1), "chg_24h_pct": chg(24 if len(closes) > 24 else 6),
            "chg_all_pct": round((last / closes[0] - 1) * 100, 2),
            "sma20": round(sma(closes, 20), 4), "above_sma20": last > sma(closes, 20),
            "rsi14": round(rsi(closes), 1), "volume_ratio": round(vols[-1] / avg_vol, 2),
            "high_10d": max(b["h"] for b in bar_list), "low_10d": min(b["l"] for b in bar_list)}


# ---------------------------------------------------------------- the six bots

def scout(held):
    """Bot 1: screen the watchlist with numbers (no AI call) and pick the most active."""
    stocks_open = market_open()
    universe = {}
    if stocks_open:
        for sym, bl in bars(STOCKS, crypto=False).items():
            universe[sym] = ("stock", bl)
    else:
        log("scout: US market closed, scanning crypto only")
    for sym, bl in bars(CRYPTO, crypto=True).items():
        universe[sym] = ("crypto", bl)

    scored = []
    for sym, (asset, bl) in universe.items():
        f = features(bl)
        if not f:
            continue
        score = abs(f["chg_24h_pct"] or 0) + 2 * abs(f["chg_1h_pct"] or 0) + max(f["volume_ratio"] - 1, 0)
        scored.append((score, sym, asset, f))
    scored.sort(reverse=True)

    picks = {sym: {"asset": asset, "features": f, "score": round(score, 2)}
             for score, sym, asset, f in scored[:SCOUT_PICKS]}
    for sym, (asset, bl) in universe.items():      # always review what we hold
        if sym.replace("/", "") in held and sym not in picks:
            f = features(bl)
            if f:
                picks[sym] = {"asset": asset, "features": f, "score": 0, "held": True}
    log(f"scout: {len(universe)} scanned, picked {', '.join(picks) or 'none'}")
    return picks


def news_bot(picks):
    """Bot 2: read the last 24h of headlines and score sentiment per symbol."""
    items = news(list(picks))
    if not items:
        return {s: {"sentiment": 0, "summary": "no recent news"} for s in picks}
    lines = [f"- [{','.join(n.get('symbols', []))}] {n.get('headline', '')}" for n in items[:40]]
    out = ask_ai(
        "You are the news analyst on a trading desk. Judge only from the headlines given.",
        f"Symbols: {', '.join(picks)}\nHeadlines (last 24h):\n" + "\n".join(lines) +
        '\n\nReturn {"<symbol exactly as listed>": {"sentiment": number -1..1, "summary": "one sentence"}} '
        "for every symbol. Use 0 when there is no relevant news.")
    return {s: out.get(s, {"sentiment": 0, "summary": "no relevant news"}) for s in picks}


def chart_bot(picks, held):
    """Bot 3: read the technicals and propose buy / sell / hold."""
    rows = {s: dict(p["features"], held=s.replace("/", "") in held) for s, p in picks.items()}
    out = ask_ai(
        "You are the technical analyst on a long-only trading desk using hourly bars. "
        "Only propose 'sell' for symbols where held is true. Be selective.",
        "Hourly-bar features per symbol:\n" + json.dumps(rows, indent=1) +
        '\n\nReturn {"<symbol>": {"action": "buy"|"sell"|"hold", "confidence": 0..1, '
        '"reason": "one sentence"}} for every symbol.')
    return out


def skeptic_bot(proposals):
    """Bot 4: argue against every proposal; veto weak ones."""
    if not proposals:
        return {}
    out = ask_ai(
        "You are the skeptic on a trading desk. Your job is to find reasons NOT to trade: "
        "conflicting news, overbought/oversold extremes, chasing a spike, thin evidence. "
        "Veto anything that is not clearly justified.",
        "Proposals:\n" + json.dumps(proposals, indent=1) +
        '\n\nReturn {"<symbol>": {"verdict": "approve"|"veto", "confidence": 0..1, '
        '"concern": "the main risk in one sentence"}} for every symbol.', heavy=True)
    return out


def sizer_bot(survivors, acct, held_count):
    """Bot 5: size each trade with fixed risk rules (no AI)."""
    equity = float(acct["equity"])
    cash = float(acct.get("non_marginable_buying_power") or acct.get("cash") or 0)
    slots = max(MAX_OPEN_POSITIONS - held_count, 0)
    sized = []
    for p in sorted(survivors, key=lambda x: -x["confidence"]):
        if p["side"] == "buy":
            if slots <= 0:
                log(f"sizer: skip {p['symbol']}, max open positions reached")
                continue
            notional = round(min(equity * MAX_POSITION_PCT / 100 * p["confidence"], cash * 0.95), 2)
            if notional < 10:
                log(f"sizer: skip {p['symbol']}, not enough cash")
                continue
            cash -= notional
            slots -= 1
            p["notional"] = notional
        else:
            p["notional"] = p.get("position_value", 0)
        sized.append(p)
        if len(sized) >= MAX_NEW_TRADES:
            break
    return sized


def executor_request(trades, dry_run):
    """Bot 6 (part 1): email each trade for approval and record it."""
    con = db()
    for t in trades:
        token = secrets.token_hex(3)
        verb = "BUY" if t["side"] == "buy" else "SELL (close)"
        subject = f"[DESK-{token}] {verb} {t['symbol']} ${t['notional']:,.2f}"
        body = (f"{verb} {t['symbol']} ({t['asset']}) for about ${t['notional']:,.2f} on the PAPER account.\n\n"
                f"Confidence after skeptic: {t['confidence']:.2f}\n"
                f"Chart: {t['chart_reason']}\n"
                f"News: {t['news_summary']} (sentiment {t['sentiment']:+.2f})\n"
                f"Skeptic: {t['concern']}\n\n"
                f"Reply with APPROVE or REJECT as the first word.\n"
                f"This request expires in {APPROVAL_MINUTES} minutes.")
        if dry_run:
            print(hide("\n--- DRY RUN, not sent ---\n" + subject + "\n" + body))
            continue
        send_email(subject, body)
        con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (token, now().isoformat(), t["symbol"], t["asset"], t["side"], t["notional"],
                     t["confidence"], body, "pending", now().isoformat(), None, None))
        con.commit()
        log(f"executor: approval requested {subject}")


def _journal(picks, sentiment, chart, concerns, outcome, dry_run):
    """Record every symbol the desk looked at, so its calls can be scored later."""
    con = db()
    at = now().isoformat()
    for sym, p in picks.items():
        f, s, c = p["features"], sentiment.get(sym, {}), chart.get(sym, {})
        con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (at, sym, p["asset"], f.get("price"), json.dumps(f),
                     float(s.get("sentiment", 0) or 0), s.get("summary", ""),
                     c.get("action", "hold"), float(c.get("confidence", 0) or 0), c.get("reason", ""),
                     concerns.get(sym, ""), outcome.get(sym, ""), int(dry_run)))
    con.commit()


def run_cycle(dry_run=False):
    acct = account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        log("account is blocked, stopping")
        return
    pos = positions()
    held = {p["symbol"]: p for p in pos}

    picks = scout(held)
    if not picks:
        log("cycle done: nothing to look at")
        return
    top = max(p.get("score", 0) for p in picks.values())
    if top < QUIET_SCORE and not held:
        log(f"cycle done: quiet market (top move score {top:.2f} < {QUIET_SCORE}), AI bots skipped")
        return
    sentiment = news_bot(picks)
    for sym in picks:
        s = sentiment.get(sym, {})
        log(f"news:  {sym:8} {float(s.get('sentiment', 0)):+.2f}  {_short(s.get('summary'))}")
    chart = chart_bot(picks, held)

    outcome = {}                          # symbol -> what happened to it this cycle
    proposals = []
    for sym in picks:
        c = chart.get(sym, {})
        action, conf = c.get("action", "hold"), float(c.get("confidence", 0) or 0)
        log(f"chart: {sym:8} {action:4} {conf:.2f}  {_short(c.get('reason'))}")
        key = sym.replace("/", "")
        if action not in ("buy", "sell"):
            outcome[sym] = "hold"
            continue
        if action == "sell" and key not in held:
            outcome[sym] = "dropped: sell but not held"
            continue
        if action == "buy" and key in held:
            outcome[sym] = "dropped: already held"
            continue                      # no adding to positions for now
        if conf < MIN_CONFIDENCE:
            outcome[sym] = f"dropped: confidence {conf:.2f} < {MIN_CONFIDENCE:.2f}"
            log(f"       {sym:8} not proposed, confidence below {MIN_CONFIDENCE:.2f}")
            continue
        outcome[sym] = "proposed"
        s = sentiment.get(sym, {})
        proposals.append({"symbol": sym, "asset": picks[sym]["asset"], "side": c["action"],
                          "chart_confidence": float(c["confidence"]), "chart_reason": c.get("reason", ""),
                          "sentiment": float(s.get("sentiment", 0)), "news_summary": s.get("summary", ""),
                          "features": picks[sym]["features"],
                          "position_value": abs(float(held[key]["market_value"])) if key in held else 0})
    log(f"chart: {len(proposals)} proposal(s)")

    verdicts = skeptic_bot([{k: v for k, v in p.items() if k != "position_value"} for p in proposals])
    survivors = []
    for p in proposals:
        v = verdicts.get(p["symbol"], {"verdict": "veto", "concern": "no verdict"})
        p["concern"] = v.get("concern", "")
        p["confidence"] = min(float(v.get("confidence", 0)), p["chart_confidence"])
        if v.get("verdict") == "approve" and p["confidence"] >= MIN_CONFIDENCE:
            survivors.append(p)
            outcome[p["symbol"]] = "skeptic approved"
            log(f"skeptic: approved {p['symbol']} at {p['confidence']:.2f} ({_short(p['concern'])})")
        else:
            outcome[p["symbol"]] = "skeptic vetoed"
            log(f"skeptic: vetoed {p['symbol']} ({_short(p['concern'])})")

    trades = sizer_bot(survivors, acct, len(held))
    for t in trades:
        outcome[t["symbol"]] = f"sent {t['side']} ${t['notional']:,.2f}"
    for p in survivors:
        if p not in trades:
            outcome[p["symbol"]] = "skipped by sizer"
    _journal(picks, sentiment, chart, {p["symbol"]: p.get("concern", "") for p in proposals},
             outcome, dry_run)
    executor_request(trades, dry_run)
    log(f"cycle done: {len(trades)} trade(s) " + ("shown (dry run)" if dry_run else "sent for approval"))


# ---------------------------------------------------------------- email

def _smtp_send(msg):
    """Try Gmail's SSL port first, then STARTTLS, retrying once each."""
    errors = []
    for port in (465, 587):
        for attempt in range(2):
            try:
                if port == 465:
                    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
                else:
                    s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                    s.ehlo(); s.starttls(); s.ehlo()
                with s:
                    try:
                        s.login(DESK_GMAIL, DESK_GMAIL_PW)
                    except smtplib.SMTPAuthenticationError as e:
                        raise RuntimeError(
                            "Gmail refused the login. Check DESK_GMAIL is the full address and "
                            "DESK_GMAIL_APP_PASSWORD is a 16-letter app password "
                            f"(code {e.smtp_code})") from None
                    s.send_message(msg)
                return
            except RuntimeError:
                raise
            except Exception as e:
                errors.append(f"port {port} try {attempt + 1}: {type(e).__name__}: {e}")
                time.sleep(3)
    raise RuntimeError("could not send via Gmail -> " + " | ".join(errors))


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, DESK_GMAIL, APPROVAL_EMAIL
    _smtp_send(msg)


def _reply_text(msg):
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None)
        if part is None:
            return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", errors="ignore")
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:$", line.strip()):
            break                          # stop at the quoted original
        lines.append(line)
    return "\n".join(lines)


def _sender_verified(msg):
    """Gmail stamps incoming mail with SPF/DKIM results; reject forged senders."""
    results = " ".join(msg.get_all("Authentication-Results") or []).lower()
    domain = APPROVAL_EMAIL.lower().split("@")[-1]
    return bool(re.search(r"\b(dmarc=pass|dkim=pass[^;]*header\.(i|d)=@?" + re.escape(domain) + ")", results))


def _imap_login_test():
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        m.login(DESK_GMAIL, DESK_GMAIL_PW)
        m.logout()
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"inbox login refused ({e}). Turn on IMAP in the desk Gmail "
                           "and check the app password") from None


def read_replies():
    """Return [(token, decision)] from unread replies sent by APPROVAL_EMAIL."""
    found = []
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(DESK_GMAIL, DESK_GMAIL_PW)
    m.select("INBOX")
    _, ids = m.search(None, "UNSEEN")
    for mid in ids[0].split():
        _, data = m.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        sender = parseaddr(msg.get("From", ""))[1].lower()
        tok = re.search(r"DESK-([0-9a-f]{6})", msg.get("Subject", ""))
        if not tok or sender != APPROVAL_EMAIL.lower():
            continue
        if not _sender_verified(msg):
            log("ignored a reply that claimed to be yours but failed Gmail's sender checks")
            m.store(mid, "+FLAGS", "\\Seen")
            continue
        first = _reply_text(msg).strip().split()
        word = first[0].upper().strip(".,!") if first else ""
        decision = "approve" if word in ("APPROVE", "APPROVED", "YES") else \
                   "reject" if word in ("REJECT", "REJECTED", "NO") else None
        if decision:
            found.append((tok.group(1), decision))
        m.store(mid, "+FLAGS", "\\Seen")
    m.logout()
    return found


# ---------------------------------------------------------------- executor

def close_position(sym, asset, reason):
    """Sell a whole position and record its profit or loss."""
    key = sym.replace("/", "")
    pos = next((p for p in positions() if p["symbol"] == key), None)
    order = http_delete(f"{TRADE_URL}/v2/positions/{key}", headers=_alpaca_headers())
    if pos:
        con = db()
        pl, pl_pct = float(pos["unrealized_pl"]), float(pos["unrealized_plpc"]) * 100
        row = con.execute("SELECT rowid FROM trades WHERE replace(symbol,'/','')=? AND closed_at IS NULL "
                          "ORDER BY opened_at DESC LIMIT 1", (key,)).fetchone()
        if row:
            con.execute("UPDATE trades SET closed_at=?, pl=?, pl_pct=?, exit_reason=? WHERE rowid=?",
                        (now().isoformat(), pl, pl_pct, reason, row[0]))
        else:
            con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
                        (sym, asset, None, abs(float(pos["cost_basis"])), now().isoformat(), pl, pl_pct, reason))
        con.commit()
    return order


def place_order(row):
    sym = row["symbol"]
    if row["side"] == "sell":
        return close_position(sym, row["asset"], "chart bot sell (you approved)")
    body = {"symbol": sym, "notional": f"{row['notional']:.2f}", "side": "buy", "type": "market",
            "time_in_force": "gtc" if row["asset"] == "crypto" else "day"}
    order = http_post(f"{TRADE_URL}/v2/orders", body, headers=_alpaca_headers())
    con = db()
    con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
                (sym, row["asset"], now().isoformat(), row["notional"], None, None, None, None))
    con.commit()
    return order


def check_exits():
    """Close positions that hit the stop-loss, take-profit or time limit."""
    if not AUTO_EXITS:
        return
    pos = positions()
    if not pos:
        return
    con = db()
    stocks_open = None
    for p in pos:
        key, crypto = p["symbol"], p.get("asset_class") == "crypto"
        pct = float(p["unrealized_plpc"]) * 100
        stop = STOP_LOSS_CRYPTO_PCT if crypto else STOP_LOSS_STOCK_PCT
        opened = con.execute("SELECT opened_at FROM trades WHERE replace(symbol,'/','')=? AND closed_at IS NULL "
                             "AND opened_at IS NOT NULL ORDER BY opened_at DESC LIMIT 1", (key,)).fetchone()
        age_days = (now() - datetime.fromisoformat(opened[0])).total_seconds() / 86400 if opened else 0
        reason = (f"stop-loss ({pct:+.1f}%, limit -{stop:g}%)" if pct <= -stop else
                  f"take-profit ({pct:+.1f}%, target +{TAKE_PROFIT_PCT:g}%)" if pct >= TAKE_PROFIT_PCT else
                  f"time limit ({age_days:.1f} days, max {MAX_HOLD_DAYS:g})" if age_days >= MAX_HOLD_DAYS else None)
        if not reason:
            continue
        if not crypto:
            if stocks_open is None:
                stocks_open = market_open()
            if not stocks_open:
                log(f"exits: {key} hit {reason}, waiting for the US market to open")
                continue
        sym = next((c for c in CRYPTO if c.replace("/", "") == key), key)
        try:
            close_position(sym, "crypto" if crypto else "stock", reason)
            log(f"exits: closed {sym} on {reason}")
            send_email(f"[desk] Closed {sym}: {reason.split(' (')[0]}",
                       f"The desk closed your {sym} position on the PAPER account.\n\n"
                       f"Reason: {reason}\nProfit/loss: ${float(p['unrealized_pl']):+,.2f} ({pct:+.2f}%)")
        except Exception as e:
            log(f"exits: could not close {sym}: {e}")


def process_approvals():
    """Bot 6 (part 2): run protective exits, act on your replies, expire old requests."""
    check_exits()
    con = db()
    if not con.execute("SELECT 1 FROM requests WHERE status='pending' LIMIT 1").fetchone():
        return                              # nothing waiting: skip the Gmail login
    for token, decision in read_replies():
        row = con.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
        if not row or row["status"] != "pending":
            continue
        age = now() - datetime.fromisoformat(row["created_at"])
        status, order_id, note = row["status"], None, None
        if decision == "reject":
            status = "rejected"
        elif age > timedelta(minutes=APPROVAL_MINUTES):
            status, note = "expired", "approved too late"
        elif row["asset"] == "stock" and not market_open():
            status, note = "expired", "market closed"
        else:
            try:
                order = place_order(row)
                status, order_id = "filled_or_sent", order.get("id")
            except Exception as e:
                status, note = "failed", str(e)[:200]
        con.execute("UPDATE requests SET status=?, order_id=?, note=?, updated_at=? WHERE token=?",
                    (status, order_id, note, now().isoformat(), token))
        con.commit()
        log(f"executor: DESK-{token} {row['side']} {row['symbol']} -> {status} {note or ''}")
        send_email(f"[DESK-{token}] {status.upper()}: {row['side']} {row['symbol']}",
                   f"Result: {status}\n{note or ''}")

    cutoff = (now() - timedelta(minutes=APPROVAL_MINUTES)).isoformat()
    n = con.execute("UPDATE requests SET status='expired', note='no reply', updated_at=? "
                    "WHERE status='pending' AND created_at < ?", (now().isoformat(), cutoff)).rowcount
    con.commit()
    if n:
        log(f"executor: {n} request(s) expired without a reply")


# ---------------------------------------------------------------- commands

def check():
    ok = True
    missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DESK_GMAIL",
                           "DESK_GMAIL_APP_PASSWORD", "APPROVAL_EMAIL") if not env(k)]
    missing += ["GEMINI_API_KEY"] if AI_PROVIDER == "gemini" and not GEMINI_KEY else []
    missing += ["ANTHROPIC_API_KEY"] if AI_PROVIDER == "claude" and not ANTHROPIC_KEY else []
    if missing:
        print("Missing in .env:", ", ".join(missing))
        return False
    for name, fn in [
        ("Alpaca paper account", lambda: f"equity ${float(account()['equity']):,.2f}"),
        ("Crypto data", lambda: f"{len(bars(CRYPTO[:1], crypto=True).get(CRYPTO[0], []))} bars for {CRYPTO[0]}"),
        (f"AI ({AI_PROVIDER})", lambda: ask_ai("Reply in JSON.", 'Return {"ok": true}')),
        ("Gmail send", lambda: (send_email("[desk] connection test", "The desk can email you."),
                                "test email sent")[-1]),
        ("Gmail inbox read", lambda: (_imap_login_test(), "inbox readable")[-1]),
    ]:
        try:
            print(f"OK    {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"FAIL  {name}: {e}")
    return ok


def status():
    a = account()
    print(f"Equity ${float(a['equity']):,.2f}   cash ${float(a['cash']):,.2f}")
    for p in positions():
        print(f"  {p['symbol']:10} value ${float(p['market_value']):>10,.2f}  "
              f"P/L {float(p['unrealized_plpc']) * 100:+.2f}%")
    for r in db().execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT 10"):
        print(f"  DESK-{r['token']} {r['created_at'][:16]} {r['side']:4} {r['symbol']:9} "
              f"${r['notional']:,.2f} {r['status']} {r['note'] or ''}")


def _ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _price_after(bar_list, when):
    for b in bar_list:
        if _ts(b["t"]) >= when:
            return b["c"]
    return None


def _group(rows, label):
    scored = [r for r in rows if r["r24"] is not None]
    if not scored:
        print(f"  {label:34} no calls old enough to score yet")
        return
    up = sum(r["r24"] > 0 for r in scored) / len(scored) * 100
    avg24 = sum(r["r24"] for r in scored) / len(scored)
    s4 = [r["r4"] for r in rows if r["r4"] is not None]
    avg4 = sum(s4) / len(s4) if s4 else 0
    print(f"  {label:34} {len(scored):4} calls  {up:5.1f}% up after 24h  "
          f"avg {avg24:+.2f}% (24h)  {avg4:+.2f}% (4h)")


def report():
    """Score every journalled call against what the price did next, then check the go-live bar."""
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM decisions ORDER BY cycle_at")]
    print("=" * 70)
    print("DESK REPORT")
    print("=" * 70)
    if not rows:
        print("No decisions in the journal yet. Let the desk run a few cycles first.")
        return
    first = _ts(rows[0]["cycle_at"])
    days = (now() - first).total_seconds() / 86400
    print(f"Journal: {len(rows)} calls since {first:%Y-%m-%d} ({days:.1f} days)\n")

    start = first - timedelta(hours=2)
    stock_syms = sorted({r["symbol"] for r in rows if r["asset"] == "stock"})
    crypto_syms = sorted({r["symbol"] for r in rows if r["asset"] == "crypto"})
    history = {}
    history.update(bars_since(stock_syms, False, start))
    history.update(bars_since(crypto_syms, True, start))
    for r in rows:
        t0, p0, bl = _ts(r["cycle_at"]), r["price"], history.get(r["symbol"], [])
        p4, p24 = _price_after(bl, t0 + timedelta(hours=4)), _price_after(bl, t0 + timedelta(hours=24))
        r["r4"] = (p4 / p0 - 1) * 100 if p4 and p0 else None
        r["r24"] = (p24 / p0 - 1) * 100 if p24 and p0 else None

    print("CHART BOT - did prices do what it said?")
    _group([r for r in rows if r["chart_action"] == "buy"], "said BUY")
    for lo, hi in ((0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)):
        _group([r for r in rows if r["chart_action"] == "buy" and lo <= r["chart_conf"] < hi],
               f"  buy, confidence {lo:.1f}-{min(hi, 1):.1f}")
    _group([r for r in rows if r["chart_action"] == "hold"], "said HOLD (baseline)")
    print("  -> BUY should beat HOLD, and higher confidence should do better.\n")

    print("SKEPTIC - were its vetoes right?")
    _group([r for r in rows if r["outcome"] == "skeptic vetoed"], "vetoed (want: low or negative)")
    _group([r for r in rows if r["outcome"] in ("skeptic approved", "skipped by sizer")
            or (r["outcome"] or "").startswith("sent")], "approved (want: positive)")
    print()

    print("NEWS BOT - did sentiment point the right way?")
    for asset in ("stock", "crypto"):
        strong = [r for r in rows if r["asset"] == asset and abs(r["sentiment"] or 0) >= 0.2
                  and r["r24"] is not None]
        if strong:
            hit = sum((r["sentiment"] > 0) == (r["r24"] > 0) for r in strong) / len(strong) * 100
            print(f"  {asset:6} {len(strong):4} strong calls, {hit:5.1f}% pointed the right way (50% = coin flip)")
        else:
            print(f"  {asset:6} not enough strong sentiment calls yet")
    print()

    trades = [dict(t) for t in con.execute("SELECT * FROM trades WHERE closed_at IS NOT NULL")]
    open_n = con.execute("SELECT count(*) FROM trades WHERE closed_at IS NULL").fetchone()[0]
    print("TRADES")
    wins = [t for t in trades if (t["pl"] or 0) > 0]
    total = sum(t["pl"] or 0 for t in trades)
    print(f"  {len(trades)} closed ({len(wins)} wins), {open_n} open, closed P/L ${total:+,.2f}")
    best_two = sorted((t["pl"] or 0 for t in trades), reverse=True)[:2]
    without_best = total - sum(best_two)
    if trades:
        print(f"  without the best two trades: ${without_best:+,.2f}")
        for reason in sorted({(t["exit_reason"] or "").split(" (")[0] for t in trades}):
            grp = [t for t in trades if (t["exit_reason"] or "").split(" (")[0] == reason]
            print(f"    {reason or 'unknown':32} {len(grp):3} trades  ${sum(t['pl'] or 0 for t in grp):+,.2f}")
    print()

    desk_ret = spy_ret = drawdown = None
    try:
        hist = http_get(f"{TRADE_URL}/v2/account/portfolio/history", headers=_alpaca_headers(),
                        params={"period": "3M", "timeframe": "1D"})
        pts = [(datetime.fromtimestamp(t, timezone.utc), e) for t, e in
               zip(hist.get("timestamp") or [], hist.get("equity") or []) if e]
        pts = [x for x in pts if x[0] >= first - timedelta(days=1)] or pts
        if len(pts) >= 2:
            eq = [e for _, e in pts]
            desk_ret = (eq[-1] / eq[0] - 1) * 100
            peak, drawdown = eq[0], 0.0
            for e in eq:
                peak = max(peak, e)
                drawdown = max(drawdown, (peak - e) / peak * 100)
            spy = bars_since(["SPY"], False, pts[0][0] - timedelta(days=3), "1Day").get("SPY", [])
            s0, s1 = _price_after(spy, pts[0][0] - timedelta(days=1)), (spy[-1]["c"] if spy else None)
            spy_ret = (s1 / s0 - 1) * 100 if s0 and s1 else None
    except Exception as e:
        print(f"  (could not read account history: {e})")
    print("ACCOUNT vs JUST HOLDING SPY")
    print(f"  desk {desk_ret:+.2f}%" if desk_ret is not None else "  desk: not enough history yet", end="")
    print(f"   SPY {spy_ret:+.2f}%" if spy_ret is not None else "", end="")
    print(f"   worst drop {drawdown:.2f}%" if drawdown is not None else "")
    print()

    checks = [
        ("3+ weeks on paper", days >= 21, f"{days:.0f} days"),
        ("15+ closed trades", len(trades) >= 15, f"{len(trades)}"),
        ("beat holding SPY", desk_ret is not None and spy_ret is not None and desk_ret > spy_ret,
         f"{desk_ret:+.2f}% vs {spy_ret:+.2f}%" if desk_ret is not None and spy_ret is not None else "n/a"),
        ("worst drop under 10%", drawdown is not None and drawdown < 10,
         f"{drawdown:.2f}%" if drawdown is not None else "n/a"),
        ("ahead without best two trades", bool(trades) and without_best > 0, f"${without_best:+,.2f}"),
    ]
    print("GO-LIVE BAR")
    for name, ok, val in checks:
        print(f"  [{'PASS' if ok else ' -- '}] {name:32} {val}")
    print("  => " + ("All passed. Talk it over before switching to live."
                     if all(ok for _, ok, _ in checks) else "Not yet. Stay on paper."))


def main(argv):
    if env("ALPACA_PAPER", "true").lower() != "true":
        sys.exit("This desk only trades paper. Set ALPACA_PAPER=true in .env.")
    cmd = argv[1] if len(argv) > 1 else "help"
    try:
        if cmd == "check":
            sys.exit(0 if check() else 1)
        elif cmd == "run":
            run_cycle(dry_run="--dry-run" in argv)
        elif cmd == "approvals":
            process_approvals()
        elif cmd == "status":
            status()
        elif cmd == "report":
            report()
        else:
            print(__doc__)
    except Exception as e:
        log(f"ERROR in {cmd}: {e}  (no trades were placed by this error)")
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
