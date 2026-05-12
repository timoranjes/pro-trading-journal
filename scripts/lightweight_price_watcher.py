#!/usr/bin/env python3
"""
Lightweight Price Watcher v2 — Merged daemon features into cron.

Combines the reliability of cron polling with features from the daemon (portfolio_monitor.py):
- Progressive threshold levels: 7% → 12% → 18% → 25%+
- Sector correlation alerts (≥2 same-sector stocks triggering within window)
- Volatility Z-score context in alerts
- Position-weighted thresholds (higher positions → lower trigger %)

Designed for no_agent=true cron execution (every 1 min):
- Runs every 1 min during market hours (all 3 markets)
- Fetches Sina prices for all portfolio positions
- Fires LLM attribution only when breach detected
- Progressive dedup: only alerts on NEW threshold levels
- Sends Discord webhook alerts with attribution

Usage:
  python3 lightweight_price_watcher.py
"""
from datetime import datetime, timezone, timedelta
import math
import subprocess
import json
import re
import sys
import requests
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from portfolio_config import SECTOR_MAP, DISCORD_CHANNEL, WEBHOOK_CACHE_PATH, get_stock_sector
from price_attribution import fetch_attribution, llm_attribution, fetch_market_context

SCRIPT_DIR = Path(__file__).parent
ALERT_HISTORY_PATH = Path.home() / ".hermes" / "data" / "price_alert_history.json"
PORTFOLIO_CACHE_PATH = Path.home() / ".hermes" / "data" / "portfolio_cache.json"
PORTFOLIO_STATE_PATH = Path.home() / ".hermes" / "portfolio_state.json"
VOL_STATE_PATH = Path.home() / ".hermes" / "data" / "volatility_state.json"
DEDUP_COOLDOWN_HOURS = 2
ALERT_PCT = 7.0  # Base threshold (Level 1)
PROGRESSIVE_LEVELS = [7.0, 12.0, 18.0, 25.0]  # Level 1-4 (percentage, matches pct values)
PROGRESSIVE_LEVEL_NAMES = ["LEVEL 1", "LEVEL 2", "LEVEL 3", "LEVEL 4+"]
SECTOR_WINDOW = 60  # seconds for sector correlation
CONSECUTIVE_REQUIRED = 1  # alert on first tick above threshold
MAX_LEVEL_REMINDER_MINUTES = 15  # re-alert interval when stuck at max level
MAX_LEVEL_REMINDER_PCT_DELTA = 3.0  # min % move since last max alert to re-alert


def now_hkt():
    return datetime.now(timezone(timedelta(hours=8)))


# === Position Weights ===
AVG_WEIGHT = 0.04
position_weights = {}

def load_position_weights():
    global position_weights, AVG_WEIGHT
    if not PORTFOLIO_STATE_PATH.exists():
        return
    try:
        state = json.loads(PORTFOLIO_STATE_PATH.read_text())
        weights = {}
        for code, info in state.items():
            w = info.get("weight")
            if w is not None:
                weights[code] = w
        if weights:
            position_weights = weights
            AVG_WEIGHT = 1.0 / len(weights)
    except Exception:
        pass

def get_threshold(code):
    """Position-weighted threshold: larger positions → lower trigger."""
    weight = position_weights.get(code, AVG_WEIGHT)
    if weight <= 0 or AVG_WEIGHT <= 0:
        return ALERT_PCT
    ratio = weight / AVG_WEIGHT
    threshold = ALERT_PCT * (1.0 / math.sqrt(ratio))
    return min(max(threshold, 0.07), ALERT_PCT)


# === Volatility State ===
volatility_data = {}

def load_volatility_state():
    global volatility_data
    if not VOL_STATE_PATH.exists():
        return
    try:
        data = json.loads(VOL_STATE_PATH.read_text())
        volatility_data = data.get("stocks", {})
    except Exception:
        pass

def get_volatility_context(code):
    """Return formatted volatility string for alert, or None."""
    vol = volatility_data.get(code)
    if vol and vol.get("returns_std"):
        z = vol.get("z_score")
        if z is not None:
            az = abs(z)
            if az < 1.0: label = "正常波动"
            elif az < 2.0: label = "波动放大"
            elif az < 3.0: label = "极端异动"
            else: label = "罕见事件"
        else:
            label = "无数据"
            z = "N/A"
        return (
            f"\n波动率: 30日波动{vol['returns_std']:.1f}% | "
            f"ATR {vol['atr']:.2f}({vol['atr_pct']:.1f}%) | "
            f"Z-Score: {z} [{label}]"
        )
    return None


# === Portfolio Positions ===
def fetch_daily_report_email():
    """Fetch the latest Daily Report (Ma Teng) email text."""
    result = subprocess.run(
        ["himalaya", "envelope", "list", "--page-size", "50", "--output", "json"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None
    emails = json.loads(result.stdout) if result.stdout.strip() else []
    report_id = None
    for email in emails:
        subj = email.get("subject", "")
        if "Daily Report" in subj and "Ma Teng" in subj:
            report_id = email["id"]
            break
    if not report_id:
        return None
    result = subprocess.run(
        ["himalaya", "message", "read", str(report_id)],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None
    return result.stdout


def parse_portfolio_from_email(email_text):
    """Parse tab-separated portfolio table from email."""
    lines = email_text.split('\n')
    stocks = []
    row_pattern = re.compile(r'^\d+\t([A-Z0-9]+\.[A-Z]{1,2})\t(.+?)\t([\d.]+)%')
    for line in lines:
        line = line.strip()
        match = row_pattern.match(line)
        if match:
            code = match.group(1)
            name = match.group(2).strip()
            weight = float(match.group(3)) / 100.0
            stocks.append({"code": code, "name": name, "weight": weight})
    return stocks


def get_positions():
    """Get portfolio positions, with 6-hour email cache."""
    if PORTFOLIO_CACHE_PATH.exists():
        age = time.time() - PORTFOLIO_CACHE_PATH.stat().st_mtime
        if age < 6 * 3600:
            return json.loads(PORTFOLIO_CACHE_PATH.read_text())
    email_text = fetch_daily_report_email()
    if not email_text:
        if PORTFOLIO_CACHE_PATH.exists():
            return json.loads(PORTFOLIO_CACHE_PATH.read_text())
        return []
    stocks = parse_portfolio_from_email(email_text)
    if stocks:
        PORTFOLIO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PORTFOLIO_CACHE_PATH.write_text(json.dumps(stocks, ensure_ascii=False, indent=2))
    return stocks


# === Market Detection ===
def get_market_type(code):
    if code.endswith(".O"):
        return "US"
    elif code.endswith(".HK"):
        return "HK"
    elif code.endswith(".SH") or code.endswith(".SZ"):
        return "A"
    return "US"


def _is_us_dst(hkt_dt):
    """Check if US Eastern is observing DST on the given HKT datetime.
    US DST: 2nd Sunday of March to 1st Sunday of November.
    When US is on DST, market hours are 1 hour earlier in HKT.
    """
    import calendar
    year = hkt_dt.year
    # 2nd Sunday of March
    march_cal = calendar.monthcalendar(year, 3)
    sundays_march = [d for d in march_cal[0] if d != 0] + [d for d in march_cal[1] if d != 0]
    if len(sundays_march) < 2:
        sundays_march = [d for week in march_cal for d in week if d != 0]
    dst_start = max(sundays_march[:2])  # 2nd Sunday
    # 1st Sunday of November
    nov_cal = calendar.monthcalendar(year, 11)
    dst_end = [d for d in nov_cal[0] if d != 0][0]  # 1st Sunday

    march_dst_start = __import__('datetime').datetime(year, 3, dst_start)
    nov_dst_end = __import__('datetime').datetime(year, 11, dst_end)
    hkt_date = hkt_dt.replace(tzinfo=None)
    return march_dst_start <= hkt_date < nov_dst_end


def is_market_open(code):
    """Check if market is open for this stock."""
    hkt = now_hkt()
    weekday = hkt.weekday()
    if weekday >= 5:
        return False
    market = get_market_type(code)
    hour = hkt.hour
    minute = hkt.minute
    hm = hour + minute / 60.0
    if market == "HK":
        return (9.5 <= hm < 12.0) or (13.0 <= hm < 16.0)
    elif market == "A":
        return (9.5 <= hm < 11.5) or (13.0 <= hm < 15.0)
    else:  # US
        is_dst = _is_us_dst(hkt)
        if is_dst:
            return 16.0 <= hm or hm < 8.0
        else:
            return 17.0 <= hm or hm < 9.0


def _is_us_premarket_hours():
    """Check if we're currently in US premarket or after-hours (not regular session)."""
    hkt = now_hkt()
    if hkt.weekday() >= 5:
        return False
    is_dst = _is_us_dst(hkt)
    hour = hkt.hour + hkt.minute / 60.0
    if is_dst:
        # DST: premarket 16:00-21:30, after-hours 21:00-04:00 next day
        return (16.0 <= hour < 21.5) or (hour < 4.0) or (hour >= 21.0)
    else:
        # Non-DST: premarket 17:00-22:30, after-hours 22:00-05:00 next day
        return (17.0 <= hour < 22.5) or (hour < 5.0) or (hour >= 22.0)


# === YFinance Premarket Fetch ===
def fetch_yfinance_premarket(codes):
    """Fetch US premarket/after-hours prices via yfinance."""
    import importlib
    try:
        yf = importlib.import_module("yfinance")
    except ImportError:
        pass  # Silent — yfinance not available
        return {}

    results = {}
    for code in codes:
        sym = code.replace('.O', '')
        try:
            t = yf.Ticker(sym)
            info = t.info
            if not info:
                continue

            # For after-hours: use regular market close as prev_close
            # yfinance regularMarketPreviousClose is unreliable (returns wrong value for some stocks)
            # Use actual historical close from last trading day
            hist = t.history(period="2d")
            if len(hist) >= 1:
                # Last completed trading day's close
                prev_close = float(hist['Close'].iloc[-1])
            else:
                prev_close = info.get('regularMarketPreviousClose') or info.get('previousClose')

            price = info.get('regularMarketPrice')
            pm_price = info.get('preMarketPrice')
            pm_pct = info.get('preMarketChangePercent')

            # Use premarket/after-hours price if available
            if pm_price and pm_price > 0:
                price = float(pm_price)

            if prev_close is None or prev_close <= 0 or price is None or price <= 0:
                continue

            pct = (price - prev_close) / prev_close * 100

            results[code] = {
                "price": float(price),
                "prev_close": float(prev_close),
                "pct": pct,
            }
            # Don't print — cron captures stdout and delivers it
        except Exception:
            pass  # Silent — errors don't need alerting

    return results


# === Sina Price Fetch ===
def fetch_sina_prices(codes):
    """Fetch prices from Sina Finance API for a list of codes."""
    if not codes:
        return {}
    sina_codes = []
    code_to_sina = {}
    for code in codes:
        m = get_market_type(code)
        if m == "US":
            sym = f"gb_{code.replace('.O', '').lower()}"
        elif m == "HK":
            sym = f"rt_hk{code.replace('.HK', '').zfill(5)}"
        elif m == "A":
            if code.endswith(".SH"):
                sym = f"sh{code.replace('.SH', '')}"
            else:
                sym = f"sz{code.replace('.SZ', '')}"
        else:
            continue
        sina_codes.append(sym)
        code_to_sina[code] = sym

    url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.encoding = "gbk"
    except Exception as e:
        pass  # Silent — Sina API error
        return {}

    results = {}
    for line in resp.text.strip().split('\n'):
        if not line.strip() or '=' not in line:
            continue
        parts = line.split('="')
        if len(parts) < 2:
            continue
        raw_sym = parts[0].split('hq_str_')[-1]
        content = parts[1].strip('";')
        if not content:
            continue
        fields = content.split(',')
        if len(fields) < 4:
            continue
        try:
            if raw_sym.startswith('rt_hk') or raw_sym.startswith('hk'):
                price = float(fields[6])
                pct = float(fields[8])
                prev_close = price / (1 + pct / 100) if (1 + pct / 100) != 0 else 0
            elif raw_sym.startswith('gb_'):
                prev_close = float(fields[26])
                # Use premarket/after-hours price if available (fields[21])
                # fields[24] = premarket time, fields[25] = regular session time
                pm_price_str = fields[21] if len(fields) > 21 else "0"
                reg_price_str = fields[1]
                pm_time_str = fields[24].strip() if len(fields) > 24 else ""
                reg_time_str = fields[25].strip() if len(fields) > 25 else ""
                try:
                    pm_price = float(pm_price_str)
                    reg_price = float(reg_price_str)
                except (ValueError, IndexError):
                    pm_price = 0
                    reg_price = 0

                # Determine if we're in premarket/after-hours by comparing timestamps
                # Parse "May 11 06:44AM EDT" format
                use_premarket = False
                if pm_price > 0 and pm_time_str and reg_time_str:
                    def parse_sina_time(s):
                        try:
                            # Remove EDT/EST suffix and add current year
                            s_clean = s.replace(" EDT", "").replace(" EST", "").strip()
                            year = now_hkt().year
                            return datetime.strptime(f"{year} {s_clean}", "%Y %b %d %I:%M%p")
                        except Exception:
                            return None
                    pm_dt = parse_sina_time(pm_time_str)
                    reg_dt = parse_sina_time(reg_time_str)
                    # If pm timestamp is more recent, use premarket price
                    if pm_dt and reg_dt and pm_dt > reg_dt:
                        use_premarket = True

                price = pm_price if use_premarket else reg_price
                pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            else:
                price = float(fields[3])
                prev_close = float(fields[2])
                pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            if price <= 0 or prev_close <= 0:
                continue
            code = None
            for c, s in code_to_sina.items():
                if s == raw_sym:
                    code = c
                    break
            if not code:
                continue
            results[code] = {"price": price, "prev_close": prev_close, "pct": pct}
        except (ValueError, IndexError):
            continue
    return results


# === Progressive Threshold State ===
def load_progressive_state():
    """Load progressive threshold tracking. Resets daily."""
    today = now_hkt().strftime("%Y-%m-%d")
    if not ALERT_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(ALERT_HISTORY_PATH.read_text())
        if data.get("date") != today:
            return {}
        return data.get("stocks", {})
    except Exception:
        return {}


def save_progressive_state(state):
    today = now_hkt().strftime("%Y-%m-%d")
    data = {"date": today, "stocks": state}
    ALERT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_HISTORY_PATH.write_text(json.dumps(data, indent=2))


def get_current_level(pct):
    """Get highest threshold level for a given % move."""
    abs_pct = abs(pct)
    for i in range(len(PROGRESSIVE_LEVELS) - 1, -1, -1):
        if abs_pct >= PROGRESSIVE_LEVELS[i]:
            return i + 1
    return 0


def should_alert_progressive(code, pct, state):
    """Check if we should alert based on progressive levels.
    Returns (should_alert, current_level).
    Alerts when: current level > last_level AND consecutive >= required.
    Also re-alerts periodically when stuck at max level (follow-up reminders).
    """
    threshold = get_threshold(code)
    direction = "up" if pct > 0 else "down"
    dir_key = f"last_level_{direction}"
    counter_key = f"consecutive_{direction}"
    current_level = get_current_level(pct)

    if code not in state:
        state[code] = {
            "last_level_up": 0, "last_level_down": 0,
            "consecutive_up": 0, "consecutive_down": 0,
        }

    abs_pct = abs(pct)
    if abs_pct >= threshold:
        state[code][counter_key] += 1
    else:
        state[code][counter_key] = 0

    last_level = state[code][dir_key]
    consecutive = state[code][counter_key]

    should = (current_level > last_level and current_level >= 1 and consecutive >= CONSECUTIVE_REQUIRED)

    # Max-level periodic reminder: when stuck at highest level, re-alert
    # every N minutes if price moved another X% since last max-level alert
    if not should and current_level == len(PROGRESSIVE_LEVELS):
        entry = state[code]
        last_max_time = entry.get(f"last_max_alert_time_{direction}")
        last_max_pct = entry.get(f"last_max_alert_pct_{direction}", 0)
        now_ts = time.time()

        time_ok = (last_max_time is None or
                   (now_ts - last_max_time) >= MAX_LEVEL_REMINDER_MINUTES * 60)
        delta_ok = (abs_pct - abs(last_max_pct)) >= MAX_LEVEL_REMINDER_PCT_DELTA

        if time_ok and delta_ok and consecutive >= CONSECUTIVE_REQUIRED:
            should = True
            # Mark this as a follow-up reminder
            entry[f"is_max_reminder_{direction}"] = True

    return should, current_level


# === Sector Correlation ===
SECTOR_ALERTS_PATH = Path.home() / ".hermes" / "data" / "sector_alerts_cache.json"

def load_sector_alerts():
    """Load recent sector alerts for correlation detection."""
    if not SECTOR_ALERTS_PATH.exists():
        return []
    try:
        data = json.loads(SECTOR_ALERTS_PATH.read_text())
        now_ts = time.time()
        # Keep only alerts within SECTOR_WINDOW
        return [a for a in data if now_ts - a["time"] < SECTOR_WINDOW]
    except Exception:
        return []


def save_sector_alerts(alerts):
    SECTOR_ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECTOR_ALERTS_PATH.write_text(json.dumps(alerts, indent=2))


def check_sector_correlation(code, sector, level):
    """If ≥2 alerts in same sector within window, return sector alert message or None."""
    recent = load_sector_alerts()
    # Add this alert
    recent.append({"code": code, "sector": sector, "level": level, "time": time.time()})
    save_sector_alerts(recent)

    # Check for sector-wide correlation
    sector_alerts = [a for a in recent if a["sector"] == sector]
    if len(sector_alerts) >= 2:
        unique_codes = set(a["code"] for a in sector_alerts)
        if len(unique_codes) >= 2:
            stocks_str = " | ".join(
                f"{a['code']} L{a['level']}" for a in sector_alerts
            )
            return f"\n\n🚨 板块联动预警 — {sector}\n{stocks_str}\n判断: 板块级别异动 ({len(unique_codes)}只持仓)"
    return None


# === LLM Attribution ===
def build_attribution_text(code, name, pct, market_context=None):
    """Call LLM attribution. Returns dict with catalyst, confidence, source."""
    market = get_market_type(code)
    ticker = code.replace(".O", "") if market == "US" else code
    try:
        data = fetch_attribution(ticker, name, pct, market)
    except Exception as e:
        print(f"  [attribution] {ticker}: fetch failed — {e}", flush=True)
        return None
    if not data.get("articles"):
        return None
    # Use passed market_context or fall back to data from fetch_attribution
    ctx = market_context or data.get("market_context")
    try:
        analysis = llm_attribution(
            data["articles"], ticker, name, pct,
            analyst_consensus=data.get("analyst_consensus"),
            earnings_proximity=data.get("earnings_proximity"),
            volume_context=data.get("volume_context"),
            market_context=ctx,
            technical_indicators=data.get("technical_indicators"),
        )
    except Exception as e:
        print(f"  [attribution] {ticker}: LLM call failed — {e}", flush=True)
        return None
    if not analysis:
        return None
    # Try parsing JSON response
    try:
        import json as _json
        # Strip markdown code blocks if present
        cleaned = analysis.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = _json.loads(cleaned)
        return {
            "headline": parsed.get("headline", parsed.get("catalyst", "无近期催化剂")),
            "detail": parsed.get("detail", ""),
            "confidence": parsed.get("confidence", ""),
            "source_title": parsed.get("source_title", ""),
            "source_url": parsed.get("source_url", ""),
        }
    except Exception:
        # Fallback: treat as plain text
        return {"headline": analysis[:120], "detail": "", "confidence": "", "source_title": "", "source_url": ""}


# === Alert Delivery ===
def send_alert_message(alert, sector_messages=None):
    """Build and print a PM-ready alert message for a SINGLE stock.
    
    Format: clean, professional, copy-paste ready. No Discord emoji spam.
    Includes portfolio impact calculation (weight × price change contribution).
    """
    now_str = now_hkt().strftime("%Y-%m-%d %H:%M HKT")
    a = alert
    
    # Portfolio impact: weight × price change = contribution to portfolio
    try:
        weight_pct = float(a.get("weight", "0").rstrip("%")) / 100
        contrib = weight_pct * a["pct"]
        contrib_str = f"{contrib:+.2f}% (仓位权重 {a.get('weight', 'N/A')})"
    except:
        contrib_str = f"权重 {a.get('weight', 'N/A')}"
    
    arrow = "▲" if a["pct"] > 0 else "▼"
    sign = "+" if a["pct"] > 0 else ""
    name = a.get("name", a["code"])
    level_name = a.get("level_name", "价格异动")
    
    lines = []
    lines.append(f"━━━ 价格异动报告 ━━━")
    lines.append(f"时间: {now_str}")
    lines.append(f"标的: {a['code']} {name}")
    if a.get("prev_price") and a.get("curr_price"):
        lines.append(f"前收→现价: {a['prev_price']:.2f} → {a['curr_price']:.2f}")
    lines.append(f"涨跌幅: {sign}{a['pct']:.1f}% | 级别: {level_name}")
    lines.append(f"组合影响: {contrib_str}")
    
    # Volatility context — condensed
    vol_ctx = a.get("volatility_context")
    if vol_ctx:
        lines.append(f"波动特征: {vol_ctx}")
    
    # Market backdrop
    market_ctx = a.get("market_context")
    if market_ctx:
        parts = []
        for idx, pct in market_ctx.items():
            s = "+" if pct >= 0 else ""
            parts.append(f"{idx} {s}{pct}%")
        lines.append(f"大盘环境: {', '.join(parts)}")
    
    # Attribution — detailed analysis
    attribution = a.get("attribution")
    if attribution and isinstance(attribution, dict):
        headline = attribution.get("headline", "")
        detail = attribution.get("detail", "")
        confidence = attribution.get("confidence", "")
        source_title = attribution.get("source_title", "")
        source_url = attribution.get("source_url", "")
        
        if headline or detail:
            lines.append("")
            lines.append("─── 驱动因素分析 ───")
            if headline:
                lines.append(f"核心结论: {headline}")
            if detail:
                lines.append(f"详细分析: {detail}")
            if source_url:
                lines.append(f"信息来源: {source_title}")
                lines.append(f"来源链接: {source_url}")
            if confidence:
                lines.append(f"可信度评估: {confidence}")
    
    # Sector correlation
    if sector_messages:
        lines.append("")
        for sm in sector_messages:
            lines.append(sm)
    
    lines.append("")
    lines.append("━━━ End ━━━")
    
    message = "\n".join(lines)
    print(message)


# === Main ===
def main():
    load_position_weights()
    load_volatility_state()

    positions = get_positions()
    if not positions:
        return

    open_codes = [p["code"] for p in positions if is_market_open(p["code"])]
    if not open_codes:
        return

    # Route US stocks to yfinance during premarket/after-hours for accurate pricing
    us_premarket = _is_us_premarket_hours()
    prices = {}
    sina_codes = []
    yf_codes = []
    for code in open_codes:
        if get_market_type(code) == "US" and us_premarket:
            yf_codes.append(code)
        else:
            sina_codes.append(code)

    # Fetch from both sources as needed
    if sina_codes:
        sina_prices = fetch_sina_prices(sina_codes)
        prices.update(sina_prices)
    if yf_codes:
        yf_prices = fetch_yfinance_premarket(yf_codes)
        prices.update(yf_prices)
        pass  # Silent — yfinance premarket fetch

    if not prices:
        return

    # Fetch market context once per run (broad market backdrop)
    market_context = fetch_market_context()

    prog_state = load_progressive_state()
    alerts = []
    sector_alerts_to_send = []

    for pos in positions:
        code = pos["code"]
        if code not in prices:
            continue

        live = prices[code]
        pct = live["pct"]
        threshold = get_threshold(code)

        if abs(pct) < threshold:
            # Reset consecutive counter
            direction = "up" if pct > 0 else "down"
            counter_key = f"consecutive_{direction}"
            if code in prog_state:
                prog_state[code][counter_key] = 0
            continue

        should, current_level = should_alert_progressive(code, pct, prog_state)
        if not should:
            continue

        # Update progressive state
        direction = "up" if pct > 0 else "down"
        dir_key = f"last_level_{direction}"
        prog_state[code][dir_key] = current_level

        # Record max-level alert timestamp/pct for follow-up reminders
        if current_level == len(PROGRESSIVE_LEVELS):
            reminder_key = f"is_max_reminder_{direction}"
            is_reminder = prog_state[code].pop(reminder_key, False)
            prog_state[code][f"last_max_alert_time_{direction}"] = time.time()
            prog_state[code][f"last_max_alert_pct_{direction}"] = abs(pct)
        else:
            is_reminder = False

        # Breach detected — alert delivered via send_alert_message

        level_name = PROGRESSIVE_LEVEL_NAMES[current_level - 1]
        if is_reminder:
            level_name = f"{level_name} (跟進)"
        next_thresh = PROGRESSIVE_LEVELS[current_level] if current_level < len(PROGRESSIVE_LEVELS) else None
        next_hint = f" → 下次预警: {next_thresh*100:.0f}%" if next_thresh else " → 已达最高级别"

        attribution = build_attribution_text(code, pos["name"], pct, market_context)

        vol_ctx = get_volatility_context(code)

        alerts.append({
            "code": code,
            "name": pos["name"],
            "weight": f"{pos['weight']*100:.0f}%",
            "prev_price": live.get("prev_close"),
            "curr_price": live.get("price"),
            "pct": round(pct, 1),
            "level_name": level_name,
            "next_hint": next_hint,
            "attribution": attribution,
            "volatility_context": vol_ctx,
            "market_context": market_context,
        })

        # Sector correlation check
        sector = get_stock_sector(code)
        if sector:
            sector_msg = check_sector_correlation(code, sector, current_level)
            if sector_msg and sector_msg not in sector_alerts_to_send:
                sector_alerts_to_send.append(sector_msg)

    if alerts:
        # Send each alert individually
        for i, alert in enumerate(alerts):
            # Attach relevant sector messages to each alert
            sector_msgs = sector_alerts_to_send if sector_alerts_to_send else None
            send_alert_message(alert, sector_msgs)
        # Silent — alert delivery handled by send_alert_message
    # Always save progressive state so consecutive counters persist across cron ticks
    save_progressive_state(prog_state)


if __name__ == "__main__":
    main()
