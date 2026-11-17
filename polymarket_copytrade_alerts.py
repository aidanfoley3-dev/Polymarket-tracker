#!/usr/bin/env python3
"""
Polymarket Top-Trader Copy-Trade Alerts
---------------------------------------
Follows the top N traders on Polymarket's PnL leaderboard and sends a push
notification to your phone every time one of them places a trade.

- Data: Polymarket Data API v2 (public, no API key needed)
    GET https://data-api.polymarket.com/v2/leaderboard
    GET https://data-api.polymarket.com/v2/activity
- Alerts: ntfy.sh (free phone app, no account) and/or Telegram, Discord.

Python 3.8+ with standard library only. No pip installs.

QUICK START
  1. Install the "ntfy" app (iOS / Android). Subscribe to a hard-to-guess
     topic name, e.g.  aidan-pm-whales-7x93k
  2. Run:
       python3 polymarket_copytrade_alerts.py --ntfy-topic aidan-pm-whales-7x93k
  3. Leave it running (laptop, Raspberry Pi, or a $5/mo VPS). Use
     `nohup ... &` or a systemd service so it survives closing the terminal.

USEFUL FLAGS
  --top 25                 how many leaderboard traders to follow
  --period all|month|week|day   leaderboard window (default: month — active
                           winners; "all" includes many dormant wallets)
  --category overall|sports|politics|crypto|...   leaderboard category
  --min-usdc 500           ignore fills smaller than this (default 250)
  --poll 20                seconds between checks (default 20)
  --telegram-token / --telegram-chat   send to a Telegram bot as well
  --discord-webhook URL    post to a Discord channel (good for your community)
  --dry-run                print alerts to the console only
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API = "https://data-api.polymarket.com/v2"
UA = {"User-Agent": "pm-copytrade-alerts/1.0", "Accept": "application/json"}
STATE_FILE = os.path.expanduser(os.getenv("STATE_FILE", "~/.pm_copytrade_state.json"))
LEADERBOARD_REFRESH_SECS = 6 * 3600


# ---------------------------------------------------------------- HTTP helpers
def get_json(path, params, retries=3):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # network blip / 429 / 5xx
            wait = 2 ** attempt * 2
            log(f"API error on {path} ({e}); retry in {wait}s")
            time.sleep(wait)
    return None


def post(url, data, headers=None):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except Exception as e:
        log(f"Notify error ({url.split('/')[2]}): {e}")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ Polymarket
def fetch_top_traders(n, period, category):
    res = get_json("/leaderboard", {
        "time_period": period, "category": category,
        "sort_by": "PNL", "limit": n,
    })
    rows = (res or {}).get("data") or []
    traders = {}
    for r in rows[:n]:
        addr = (r.get("user_id") or "").lower()
        if addr:
            traders[addr] = {
                "rank": r.get("rank"),
                "name": r.get("user_name") or r.get("x_username") or addr[:8],
                "pnl": r.get("pnl") or 0,
            }
    return traders


def fetch_new_trades(wallet, since_ts):
    """TRADE rows for a wallet at/after since_ts, newest first."""
    res = get_json("/activity", {
        "user": wallet, "type": "TRADE", "start": since_ts,
        "limit": 200, "sort_direction": "DESC",
    })
    return (res or {}).get("data") or []


def trade_key(t):
    return "|".join(str(t.get(k, "")) for k in
                    ("transaction_hash", "token_id", "side", "size", "price"))


# ------------------------------------------------------------------- Alerts
def group_fills(trades):
    """One big order usually fills in many pieces. Merge fills on the same
    market/outcome/side into a single alert with a size-weighted avg price."""
    groups = defaultdict(lambda: {"usdc": 0.0, "shares": 0.0, "row": None, "ts": 0})
    for t in trades:
        k = (t.get("condition_id"), t.get("outcome"), t.get("side"))
        g = groups[k]
        g["usdc"] += float(t.get("usdc_size") or 0)
        g["shares"] += float(t.get("size") or 0)
        g["ts"] = max(g["ts"], int(t.get("timestamp") or 0))
        g["row"] = g["row"] or t
    return list(groups.values())


def format_alert(trader, g):
    t = g["row"]
    avg = g["usdc"] / g["shares"] if g["shares"] else float(t.get("price") or 0)
    side = t.get("side", "?")
    emoji = "🟢" if side == "BUY" else "🔴"
    age = int(time.time()) - g["ts"]
    slug = t.get("event_slug") or t.get("slug") or ""
    url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    title = f"{emoji} #{trader['rank']} {trader['name']} {side} {t.get('outcome','')}"
    body = (
        f"{t.get('title','(market)')}\n"
        f"${g['usdc']:,.0f} @ {avg*100:.1f}¢  ({g['shares']:,.0f} sh)\n"
        f"Trader PnL: ${trader['pnl']:,.0f}  |  {age}s ago"
    )
    return title, body, url


def notify(cfg, title, body, url):
    if cfg.dry_run:
        print(f"\n{title}\n{body}\n{url}\n", flush=True)
        return
    if cfg.ntfy_topic:
        q = urllib.parse.urlencode({"title": title, "click": url, "priority": "high"})
        post(f"{cfg.ntfy_server}/{cfg.ntfy_topic}?{q}", body.encode())
    if cfg.telegram_token and cfg.telegram_chat:
        post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
             {"chat_id": cfg.telegram_chat, "text": f"{title}\n{body}\n{url}",
              "disable_web_page_preview": True},
             {"Content-Type": "application/json"})
    if cfg.discord_webhook:
        post(cfg.discord_webhook, {"content": f"**{title}**\n{body}\n<{url}>"},
             {"Content-Type": "application/json"})


# ------------------------------------------------------------------- State
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            s["seen"] = set(s.get("seen", []))
            return s
    except Exception:
        return {"seen": set(), "last_ts": {}}


def save_state(s):
    seen = list(s["seen"])[-20000:]  # cap file size
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": seen, "last_ts": s["last_ts"]}, f)


# -------------------------------------------------------------------- Main
def run(cfg):
    state = load_state()
    traders, lb_fetched = {}, 0
    started = time.time()
    log(f"Starting. Following top {cfg.top} ({cfg.period}/{cfg.category}), "
        f"min ${cfg.min_usdc}, poll {cfg.poll}s")

    while True:
        if cfg.max_runtime and time.time() - started > cfg.max_runtime:
            save_state(state)
            log("Max runtime reached; exiting cleanly for hand-off.")
            return
        if time.time() - lb_fetched > LEADERBOARD_REFRESH_SECS or not traders:
            fresh = fetch_top_traders(cfg.top, cfg.period, cfg.category)
            if fresh:
                added = set(fresh) - set(traders)
                traders, lb_fetched = fresh, time.time()
                log(f"Leaderboard loaded: {len(traders)} traders"
                    + (f" ({len(added)} new)" if added and lb_fetched else ""))
                for a in added:  # don't spam history for newly added wallets
                    state["last_ts"].setdefault(a, int(time.time()) - cfg.lookback)
            elif not traders:
                log("Couldn't load leaderboard; retrying in 60s")
                time.sleep(60)
                continue

        for wallet, info in traders.items():
            since = int(state["last_ts"].get(wallet, time.time()))
            rows = fetch_new_trades(wallet, since)
            fresh = [r for r in rows if trade_key(r) not in state["seen"]]
            for r in fresh:
                state["seen"].add(trade_key(r))
            if fresh:
                state["last_ts"][wallet] = max(int(r.get("timestamp") or 0) for r in fresh)
            for g in group_fills(fresh):
                if g["usdc"] < cfg.min_usdc:
                    continue
                title, body, url = format_alert(info, g)
                log(title.replace("\n", " "))
                notify(cfg, title, body, url)
            time.sleep(0.3)  # be polite to the API

        save_state(state)
        time.sleep(cfg.poll)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # Every flag can also come from an environment variable (used by the
    # GitHub Actions cloud setup, where values are stored as Secrets).
    env = lambda k, d=None: os.getenv(k) or d
    p.add_argument("--top", type=int, default=int(env("TOP", 25)))
    p.add_argument("--period", default=env("PERIOD", "month"),
                   choices=["day", "week", "month", "all"])
    p.add_argument("--category", default=env("CATEGORY", "overall"))
    p.add_argument("--min-usdc", type=float, default=float(env("MIN_USDC", 250)))
    p.add_argument("--poll", type=int, default=int(env("POLL", 20)))
    p.add_argument("--max-runtime", type=int, default=int(env("MAX_RUNTIME", 0)),
                   help="exit after N seconds (0 = run forever)")
    p.add_argument("--lookback", type=int, default=int(env("LOOKBACK", 0)),
                   help="on first sight of a wallet, also alert trades from the last N seconds")
    p.add_argument("--ntfy-topic", default=env("NTFY_TOPIC"))
    p.add_argument("--ntfy-server", default=env("NTFY_SERVER", "https://ntfy.sh"))
    p.add_argument("--telegram-token", default=env("TELEGRAM_TOKEN"))
    p.add_argument("--telegram-chat", default=env("TELEGRAM_CHAT"))
    p.add_argument("--discord-webhook", default=env("DISCORD_WEBHOOK"))
    p.add_argument("--dry-run", action="store_true")
    cfg = p.parse_args()
    if not (cfg.dry_run or cfg.ntfy_topic or cfg.telegram_token or cfg.discord_webhook):
        p.error("pick at least one alert channel (e.g. --ntfy-topic) or use --dry-run")
    try:
        run(cfg)
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
