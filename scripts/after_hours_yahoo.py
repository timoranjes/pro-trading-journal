#!/usr/bin/env python3
"""
After-Hours Price Monitor v6 — yfinance
Data sources:
  1. yfinance: postMarketPrice/preMarketPrice (primary)
  2. Finnhub: Fallback if yfinance fails

Strategy:
  - yfinance handles rate limiting internally
  - Uses ticker.info['postMarketPrice'] for true after-hours prices
  - Falls back to Finnhub if yfinance unavailable
  - Cache for 5 min to avoid redundant fetches
"""

import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SYMBOLS = ["MU", "SNDK", "LITE"]
ALERT_THRESHOLD = 0.07  # 7% threshold
STATE_DIR = Path.home() / ".hermes" / "data"
ALERT_HISTORY_PATH = STATE_DIR / "us_extended_alerts.json"
CACHE_PATH = STATE_DIR / "yahoo_ah_cache.json"
CACHE_TTL_SECONDS = 60  # 1 min (aligned with 1-min polling for 7:00-08:00 HKT window)

FINNHUB_KEY = "d6n74gpr01qir35jdoagd6n74gpr01qir35jdob0"


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def save_cache(data: dict):
    CACHE_DIR = Path.home() / ".hermes" / "data"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, indent=2))


def fetch_yfinance(symbol: str) -> dict:
    """Fetch after-hours/pre-market price from yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.info
        
        reg_price = info.get("regularMarketPrice")
        prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
        post_price = info.get("postMarketPrice")
        post_pct = info.get("postMarketChangePercent")
        pre_price = info.get("preMarketPrice")
        
        # Determine which price to use
        if post_price and post_price > 0:
            price = post_price
            is_ah = True
            change_pct = post_pct if post_pct is not None else ((post_price - prev_close) / prev_close * 100 if prev_close else 0)
        elif pre_price and pre_price > 0:
            price = pre_price
            is_ah = True
            change_pct = ((pre_price - prev_close) / prev_close * 100 if prev_close else 0)
        elif reg_price and reg_price > 0:
            price = reg_price
            is_ah = False
            change_pct = ((reg_price - prev_close) / prev_close * 100 if prev_close else 0)
        else:
            return None
        
        return {
            "price": price,
            "prev_close": prev_close or 0,
            "change_pct": round(change_pct, 2),
            "is_after_hours": is_ah,
            "last_trade_time": "",
            "after_hours_trade_count": 0,
            "fetched_at": datetime.now().timestamp(),
            "from_cache": False,
            "stale": False,
        }
    except Exception:
        return None


def fetch_finnhub_quote(symbol: str) -> dict:
    """Fallback: Finnhub for regular quote."""
    try:
        import requests
        r = requests.get(
            f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "price": data.get("c", 0),
                "prev_close": data.get("pc", 0),
                "change_pct": round(data.get("dp", 0), 2),
                "is_after_hours": False,
                "last_trade_time": "",
                "after_hours_trade_count": 0,
                "fetched_at": datetime.now().timestamp(),
                "from_cache": False,
                "stale": True,
                "fallback": True,
            }
    except Exception:
        pass
    return None


def fetch_with_cache(symbol: str) -> dict:
    """Fetch with smart caching + stale cleanup."""
    cache = load_cache()
    
    # Clean stale cache entries (>2x TTL)
    now_ts = datetime.now().timestamp()
    stale = [s for s, c in cache.items() if now_ts - c.get("fetched_at", 0) > CACHE_TTL_SECONDS * 2]
    for s in stale:
        del cache[s]
    if stale:
        save_cache(cache)
    
    # Check cache
    if symbol in cache:
        cached = cache[symbol]
        age = now_ts - cached.get("fetched_at", 0)
        if age < CACHE_TTL_SECONDS:
            cached["from_cache"] = True
            cached["stale"] = False
            return cached
    
    # Try yfinance first
    data = fetch_yfinance(symbol)
    
    if data:
        cache[symbol] = data
        save_cache(cache)
        return data
    
    # Fallback to Finnhub
    finnhub = fetch_finnhub_quote(symbol)
    if finnhub:
        cache[symbol] = finnhub
        save_cache(cache)
        return finnhub
    
    # All failed, try stale cache
    if symbol in cache:
        cached = cache[symbol]
        cached["from_cache"] = True
        cached["stale"] = True
        cached["fallback"] = True
        return cached
    
    return {"error": "All sources unavailable"}


def load_alert_history() -> set:
    if ALERT_HISTORY_PATH.exists():
        try:
            data = json.loads(ALERT_HISTORY_PATH.read_text())
            if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                return set(data.get("fired", []))
        except Exception:
            pass
    return set()


def save_alert(fired_set: set):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_HISTORY_PATH.write_text(json.dumps({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "fired": list(fired_set),
    }, indent=2))


def main():
    now = datetime.now()
    if 4 <= now.hour < 8:
        session = "after-hours"
    elif 16 <= now.hour < 21:
        session = "pre-market"
    elif 21 <= now.hour or now.hour < 4:
        session = "regular"
    else:
        session = "unknown"
    
    # Clear stale cache
    cache = load_cache()
    now_ts = now.timestamp()
    stale_symbols = [sym for sym, c in cache.items() if now_ts - c.get("fetched_at", 0) > CACHE_TTL_SECONDS * 2]
    for sym in stale_symbols:
        del cache[sym]
    if stale_symbols:
        save_cache(cache)
        print(f"  🗑️ Cleared stale cache for: {', '.join(stale_symbols)}", flush=True)
    
    print(f"=== After-Hours Monitor @ {now.strftime('%Y-%m-%d %H:%M')} HKT ===", flush=True)
    print(f"  Cache TTL: {CACHE_TTL_SECONDS//60} min", flush=True)

    alerted = load_alert_history()
    alerts = []

    for i, sym in enumerate(SYMBOLS):
        # Random jitter 0–3s
        if i > 0:
            time.sleep(random.uniform(0, 3))
        
        data = fetch_with_cache(sym)
        
        if "error" in data:
            print(f"  {sym}: ⚠️ {data['error']}", flush=True)
            continue
        
        direction = "📈" if data["change_pct"] > 0 else "📉"
        
        # Build status flag
        if data.get("stale"):
            status = "[AH ⚠️]" if data.get("is_after_hours") else "[Reg ⚠️]"
        elif data.get("is_after_hours"):
            status = "[AH]"
        else:
            status = "[Reg]"
        
        detail = (
            f"${data['price']:.2f} (prev: ${data['prev_close']:.2f}, "
            f"{direction} {data['change_pct']:+.2f}%) {status}"
        )
        if data.get("last_trade_time"):
            detail += f" — last: {data['last_trade_time']}"
        if data.get("after_hours_trade_count", 0) > 0:
            detail += f" ({data['after_hours_trade_count']} trades)"
        
        print(f"  {sym}: {detail}", flush=True)
        
        # Alert on moves >= 7%
        if abs(data["change_pct"]) >= ALERT_THRESHOLD * 100:
            key = f"{sym}:{'up' if data['change_pct'] > 0 else 'down'}:{session}"
            if key not in alerted:
                alerted.add(key)
                alerts.append({
                    "symbol": sym,
                    "price": data["price"],
                    "change_pct": data["change_pct"],
                    "session": session,
                    "is_after_hours": data.get("is_after_hours", False),
                })
    
    save_alert(alerted)
    
    if len(alerts) == 0:
        print("\n[SILENT]", flush=True)
        return
    
    print(f"\n  ⚠️ {len(alerts)} alert(s) fired:", flush=True)
    for a in alerts:
        direction = "📈" if a["change_pct"] > 0 else "📉"
        print(f"    {a['symbol']}: ${a['price']:.2f} {direction} {a['change_pct']:+.2f}% ({a['session']})", flush=True)


if __name__ == "__main__":
    main()
