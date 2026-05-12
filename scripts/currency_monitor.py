#!/usr/bin/env python3
"""
Currency Risk Overlay (v4):
- Monitors USDCNY and USDHKD via yfinance
- Alerts on ≥2% daily FX moves
- Calculates currency impact on portfolio positions
- Sends alerts to Discord #portfolio-alerts
"""

import sys
import json
import requests
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)

# --- Configuration ---
PORTFOLIO_STATE_PATH = Path.home() / ".hermes" / "portfolio_state.json"
DISCORD_CHANNEL = "1502241038579011655"
WEBHOOK_CACHE_PATH = Path.home() / ".hermes" / "data" / "webhooks.json"
FX_THRESHOLD = 0.02  # 2% daily move

# yfinance FX tickers
FX_TICKERS = {
    "USDCNY": "USDCNY=X",
    "USDHKD": "USDHKD=X",
}

# Currency classification for portfolio stocks
CURRENCY_CLASSIFICATION = {
    ".O": "USD",   # US-listed stocks
    ".HK": "HKD",  # HK-listed stocks
    ".SH": "CNY",  # A-shares Shanghai
    ".SZ": "CNY",  # A-shares Shenzhen
}


def get_webhook_url(channel_id):
    """Load webhook URL from cache."""
    try:
        if WEBHOOK_CACHE_PATH.exists():
            cache = json.loads(WEBHOOK_CACHE_PATH.read_text())
            return cache.get(channel_id)
    except Exception as e:
        print(f"  Webhook cache error: {e}", flush=True)
    return None


def send_discord(msg):
    """Send message via webhook (no bot token needed)."""
    webhook_url = get_webhook_url(DISCORD_CHANNEL)
    if not webhook_url:
        print("  No webhook URL found for channel, skipping alert.", flush=True)
        return
    try:
        r = requests.post(webhook_url, json={"content": msg}, timeout=10)
        if r.status_code in (200, 204):
            print("  Alert sent to Discord via webhook", flush=True)
        else:
            print(f"  Webhook error: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  Webhook network error: {e}", flush=True)


def load_portfolio_state():
    if not PORTFOLIO_STATE_PATH.exists():
        print("  portfolio_state.json not found", flush=True)
        return {}
    try:
        return json.loads(PORTFOLIO_STATE_PATH.read_text())
    except Exception as e:
        print(f"  Error loading portfolio state: {e}", flush=True)
        return {}


def get_stock_currency(code):
    """Determine currency from stock code suffix."""
    for suffix, currency in CURRENCY_CLASSIFICATION.items():
        if code.endswith(suffix):
            return currency
    return "CNY"  # default


def fetch_fx_sina():
    """Fetch FX data from Sina Finance (no auth needed).
    Returns dict: {pair_name: {prev_close, close, change_pct}}
    """
    results = {}
    # Sina FX symbols (direct currency pair codes)
    sina_map = {
        "USDCNY": "USDCNY",      # USD/CNY
        "USDHKD": "USDHKD",      # USD/HKD
    }
    url = "http://hq.sinajs.cn/list=" + ",".join(sina_map.values())
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'gbk'
        for line in r.text.split('\n'):
            if not line.strip():
                continue
            parts = line.split('="')
            if len(parts) < 2:
                continue
            raw_sym = parts[0].split('hq_str_')[-1]
            content = parts[1].strip('";')
            fields = content.split(',')
            # Sina FX fields: time,open,prev_close,high,volume,low,close,ask,bid,name,date
            if len(fields) < 7:
                continue
            try:
                prev_close = float(fields[2]) if fields[2] else 0
                close = float(fields[6]) if fields[6] else 0
            except (ValueError, IndexError):
                continue
            if close <= 0 or prev_close <= 0:
                continue
            # Map back to pair name
            pair_name = None
            for pn, sym in sina_map.items():
                if sym == raw_sym:
                    pair_name = pn
                    break
            if not pair_name:
                continue
            change_pct = (close - prev_close) / prev_close
            results[pair_name] = {
                'prev_close': prev_close,
                'close': close,
                'change_pct': change_pct,
            }
            print(f"  {pair_name} (Sina): {prev_close:.4f} → {close:.4f} ({change_pct*100:+.2f}%)", flush=True)
    except Exception as e:
        print(f"  Sina FX fetch error: {e}", flush=True)
    return results


def fetch_fx_yfinance():
    """Fallback: fetch FX data from yfinance."""
    results = {}
    try:
        import yfinance as yf
    except ImportError:
        return results

    for pair_name, ticker in FX_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist.empty or len(hist) < 2:
                continue
            prev_close = hist['Close'].iloc[-2]
            close = hist['Close'].iloc[-1]
            change_pct = (close - prev_close) / prev_close
            results[pair_name] = {
                'prev_close': prev_close,
                'close': close,
                'change_pct': change_pct,
            }
            print(f"  {pair_name} (yfinance): {prev_close:.4f} → {close:.4f} ({change_pct*100:+.2f}%)", flush=True)
        except Exception as e:
            print(f"  {pair_name} yfinance error: {e}", flush=True)
    return results


def fetch_fx_data():
    """Fetch latest FX data. Tries Sina first, falls back to yfinance.
    Returns dict: {pair_name: {prev_close, close, change_pct}}
    """
    # Try Sina Finance first (no auth, always available)
    results = fetch_fx_sina()
    if results:
        return results

    # Fallback to yfinance
    print("  Sina FX unavailable, trying yfinance fallback...", flush=True)
    results = fetch_fx_yfinance()
    if not results:
        print("  No FX data available from any source", flush=True)
    return results


def main():
    print(f"=== 汇率风险监测 @ {datetime.now().isoformat()} ===", flush=True)

    # Fetch FX data
    fx_data = fetch_fx_data()
    if not fx_data:
        print("  无汇率数据", flush=True)
        return

    # Check for alerts
    usdcny_change = fx_data.get("USDCNY", {}).get("change_pct", 0)
    usdhkd_change = fx_data.get("USDHKD", {}).get("change_pct", 0)

    alert_triggered = False
    if abs(usdcny_change) >= FX_THRESHOLD:
        alert_triggered = True
        print(f"  ⚠️ USDCNY波动: {usdcny_change*100:+.2f}% (阈值: ±{FX_THRESHOLD*100:.0f}%)", flush=True)
    if abs(usdhkd_change) >= FX_THRESHOLD:
        alert_triggered = True
        print(f"  ⚠️ USDHKD波动: {usdhkd_change*100:+.2f}% (阈值: ±{FX_THRESHOLD*100:.0f}%)", flush=True)

    # Load portfolio and calculate currency impact
    state = load_portfolio_state()
    currency_exposure = {"USD": {"weight": 0.0, "impact": 0.0}, "HKD": {"weight": 0.0, "impact": 0.0}, "CNY": {"weight": 0.0, "impact": 0.0}}

    if state:
        for code, info in state.items():
            currency = get_stock_currency(code)
            weight = info.get("weight", 0)
            currency_exposure[currency]["weight"] += weight

        # Calculate currency impact on portfolio
        # USD: currency impact = usdcny_change on US stocks (if USD weakens vs CNY, CNY value drops)
        currency_exposure["USD"]["impact"] = usdcny_change * currency_exposure["USD"]["weight"]
        currency_exposure["HKD"]["impact"] = usdhkd_change * currency_exposure["HKD"]["weight"]
        currency_exposure["CNY"]["impact"] = 0.0  # base currency

    if alert_triggered:
        date_str = datetime.now().strftime("%Y-%m-%d")
        msg = f"💱 **汇率风险预警** | {date_str}\\n"

        for pair_name in ["USDCNY", "USDHKD"]:
            data = fx_data.get(pair_name)
            if data:
                msg += f"{pair_name}: {data['prev_close']:.4f} → {data['close']:.4f} (**{data['change_pct']*100:+.2f}%**)\\n"

        msg += f"\\n组合汇率影响:\\n"
        for currency in ["USD", "HKD"]:
            exp = currency_exposure.get(currency, {})
            fx_change = usdcny_change if currency == "USD" else usdhkd_change
            portfolio_impact = exp.get("impact", 0)
            label = "美股资产" if currency == "USD" else "港股资产"
            msg += f"  {currency} ({label}): 汇率变动{fx_change*100:+.2f}% → 对组合影响{portfolio_impact*100:+.2f}%\\n"

        msg += f"\\n_阈值: ≥{FX_THRESHOLD*100:.0f}% 单日汇率波动_"
        print(msg, flush=True)
        send_discord(msg)
    else:
        # Stable — silent (no stdout for no_agent cron jobs)
        sys.exit(0)


if __name__ == "__main__":
    main()
