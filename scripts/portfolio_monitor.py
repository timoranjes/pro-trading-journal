#!/usr/bin/env python3
"""
Portfolio Monitor v5.0 — Sina-Only Polling
Monitors US/HK/A-share portfolio holdings with position-weighted thresholds.

Data Source:
  Sina Finance — A-share, HK, US (single polling loop, 3s interval)

Key features:
  - Position-weighted progressive thresholds (4 levels)
  - Volatility context with Z-Score filter
  - Sector correlation alerts
  - Price attribution (inline)
"""

import sys
import json
import math
import requests
import threading
import time
import traceback
from datetime import datetime, time as _dt_time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

# Create a requests session that bypasses the system proxy
# (Some VPN/security software on port 10808 blocks Sina API)
session = requests.Session()
session.trust_env = False  # Ignore system proxy settings

# === Path Constants ===
PORTFOLIO_STATE_PATH = Path.home() / ".hermes" / "portfolio_state.json"
VOL_STATE_PATH = Path.home() / ".hermes" / "data" / "volatility_state.json"

# === Time-decay filter ===
TIME_DECAY_CONSECUTIVE = 2  # Require N consecutive ticks above threshold

# === Module aliases ===
_time_module = time
_datetime_time = _dt_time

# Auto-generated sector mapping - DO NOT EDIT MANUALLY
# Run: python3 auto_sector_classify.py --update to regenerate
SECTOR_MAP = {
    'AI/算力/云': ['2513.HK', '601138.SH', '7709.HK'],
    'PCB/覆铜板': ['002384.SZ', '1888.HK', '300476.SZ', '3200.HK', '600183.SH', '688700.SH'],
    '光纤光缆': ['6869.HK'],
    '光通信/光模块': ['300308.SZ', '300502.SZ', 'LITE.O'],
    '半导体/存储': ['MU.O', 'SNDK.O'],
    '威胜': ['3393.HK'],
    '射频/天线': ['300136.SZ'],
    '工业/制造': ['002353.SZ', '600482.SH', '603268.SH', '688808.SH'],
    '新能源/电池': ['300438.SZ', '300750.SZ'],
    '材料/化工': ['3858.HK'],
    '材料/玻纤': ['600176.SH'],
    '电力设备': ['002028.SZ'],
    '黄金/贵金属': ['2259.HK'],
}

# === FEATURE 1: Position-Weighted Thresholds ===
position_weights = {}  # {code: weight}
AVG_WEIGHT = 0.04  # will be recalculated from portfolio

def load_position_weights():
    """Load position weights from portfolio_state.json."""
    global position_weights, AVG_WEIGHT
    if not PORTFOLIO_STATE_PATH.exists():
        return
    try:
        state = json.loads(PORTFOLIO_STATE_PATH.read_text())
        weights = {}
        for code, info in state.items():
            w = info.get('weight')
            if w is not None:
                weights[code] = w
        if weights:
            position_weights = weights
            AVG_WEIGHT = 1.0 / len(weights)
            log(f"   Loaded position weights for {len(weights)} stocks (avg_weight={AVG_WEIGHT:.4f})")
    except Exception as e:
        log(f"   Weight load error: {e}")

def get_threshold(code):
    """Return per-symbol threshold based on position weight.
    Formula: threshold = ALERT_THRESHOLD * (1 / sqrt(weight / avg_weight))
    Clamp: min 7% (smallest positions), max ALERT_THRESHOLD (7%)
    """
    weight = position_weights.get(code, AVG_WEIGHT)
    if weight <= 0 or AVG_WEIGHT <= 0:
        return ALERT_THRESHOLD
    ratio = weight / AVG_WEIGHT
    threshold = ALERT_THRESHOLD * (1.0 / math.sqrt(ratio))
    # Clamp: min 7% (smallest positions), max 10% (never exceed base threshold)
    return min(max(threshold, 0.07), ALERT_THRESHOLD)

def load_portfolio_symbols():
    """Load symbols dynamically from portfolio state.

    Returns:
      Sina-formatted symbols dict: {"US": [...], "HK": [...], "A": [...]}
    """
    sina_defaults = {
        "US": ["gb_mu", "gb_sndk", "gb_lite"],
        "HK": ["rt_hk01888", "rt_hk02259", "rt_hk02513", "rt_hk03200", "rt_hk03393", "rt_hk03858", "rt_hk06869", "rt_hk07709"],
        "A": ["sz002028", "sz002353", "sz002384", "sz300136", "sz300308", "sz300476", "sz300502", "sz300750",
              "sh600176", "sh600183", "sh600482", "sh601138", "sh603268", "sh688700"]
    }

    if not PORTFOLIO_STATE_PATH.exists():
        return sina_defaults

    try:
        state = json.loads(PORTFOLIO_STATE_PATH.read_text())
        us_sina, hk_sina, a_sina = [], [], []

        for code in state:
            if code.endswith(".O"):
                ticker = code.replace(".O", "").lower()
                us_sina.append(f"gb_{ticker}")
            elif code.endswith(".HK"):
                hk_code = code.replace('.HK', '')
                hk_sina.append(f"rt_hk{hk_code.zfill(5)}")
            elif code.endswith(".SH"):
                a_code = code.replace('.SH', '')
                a_sina.append(f"sh{a_code}")
            elif code.endswith(".SZ"):
                a_code = code.replace('.SZ', '')
                a_sina.append(f"sz{a_code}")

        return {
            "US": us_sina or sina_defaults["US"],
            "HK": hk_sina or sina_defaults["HK"],
            "A": a_sina or sina_defaults["A"],
        }
    except:
        return sina_defaults

_sina_symbols = load_portfolio_symbols()
SINA_SYMBOLS_US = _sina_symbols["US"]
SINA_SYMBOLS_HK = _sina_symbols["HK"]
SINA_SYMBOLS_A = _sina_symbols["A"]
SINA_ALL_SYMBOLS = SINA_SYMBOLS_US + SINA_SYMBOLS_HK + SINA_SYMBOLS_A

ALERT_THRESHOLD = 0.07  # base threshold (Level 1)
PROGRESSIVE_LEVELS = [0.07, 0.12, 0.18, 0.25]  # Level 1-4 thresholds (decimal, matches pct as decimal)
PROGRESSIVE_LEVEL_NAMES = ["LEVEL 1", "LEVEL 2", "LEVEL 3", "LEVEL 4+"]
DISCORD_CHANNEL = "1502241038579011655"  # #portfolio-alerts (restored)
DISCORD_TOKEN_PATH = Path.home() / ".hermes" / ".env"
ALERT_HISTORY_PATH = Path.home() / ".hermes" / "data" / "alert_history.json"
PROGRESSIVE_STATE_PATH = Path.home() / ".hermes" / "data" / "progressive_thresholds.json"
HOLIDAY_CACHE_PATH = Path.home() / ".hermes" / "data" / "holiday_cache.json"
HOLIDAY_CACHE_TTL = 3600  # Cache holidays for 1 hour

# Global State
state_lock = threading.Lock()
state = {}
progressive_state = {}  # Progressive threshold tracking: {code: {last_level_up, last_level_down, last_time_up, last_time_down}}
recent_alerts = []  # For sector correlation: list of {code, name, pct, dir, time, level}
SECTOR_WINDOW = 60  # seconds

# === FEATURE 1: Load Volatility State ===
volatility_data = {}

def load_volatility_state():
    """Load pre-computed volatility data."""
    global volatility_data
    if not VOL_STATE_PATH.exists():
        return
    try:
        data = json.loads(VOL_STATE_PATH.read_text())
        volatility_data = data.get("stocks", {})
        print(f"   Loaded volatility data for {len(volatility_data)} stocks", flush=True)
    except Exception as e:
        print(f"   Volatility load error: {e}", flush=True)


def get_market_holidays():
    """Fetch holidays from local trading_holidays.py (authoritative for CN/extended holidays).
    Falls back to nager.at API for HK/US if local file unavailable.
    Returns {country: set(date_str)}.
    """
    now = _time_module.time()
    cache = {}
    if HOLIDAY_CACHE_PATH.exists():
        try:
            cache = json.loads(HOLIDAY_CACHE_PATH.read_text())
            if now - cache.get("ts", 0) < HOLIDAY_CACHE_TTL:
                return cache.get("holidays", {})
        except:
            cache = {}

    holidays = {}

    # Try loading from local trading_holidays.py first (has CN extended holidays)
    try:
        import importlib.util
        import runpy
        holidays_path = Path.home() / ".hermes" / "scripts" / "trading_holidays.py"
        if holidays_path.exists():
            try:
                # Import via runpy to avoid sys.module caching issues
                th = runpy.run_path(str(holidays_path))
                data = th.get("load_holiday_data", lambda: th.get("HOLIDAY_DATA", {}))()
                if isinstance(data, dict) and "markets" in data:
                    for market_key, market in data["markets"].items():
                        country_map = {"a_share": "CN", "hk": "HK", "us": "US"}
                        country = country_map.get(market_key, market_key)
                        dates = set(h["date"] for h in market["holidays"])
                        for hd in market.get("half_days", []):
                            dates.add(hd["date"])
                        holidays[country] = dates
            except Exception as e:
                print(f"   Local holiday load via runpy error: {e}, trying importlib...", flush=True)
                # Fallback: try importlib (older method)
                spec = importlib.util.spec_from_file_location(
                    "trading_holidays", holidays_path)
                if spec and spec.loader:
                    th = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(th)
                    data = th.load_holiday_data()
                    for market_key, market in data["markets"].items():
                        country_map = {"a_share": "CN", "hk": "HK", "us": "US"}
                        country = country_map.get(market_key, market_key)
                        dates = set(h["date"] for h in market["holidays"])
                        for hd in market.get("half_days", []):
                            dates.add(hd["date"])
                        holidays[country] = dates
    except Exception as e:
        print(f"   Local holiday load error: {e}, falling back to API", flush=True)

    if not holidays:
        # Fallback: fetch from nager.at API
        try:
            today = datetime.now()
            for country in ["CN", "HK", "US"]:
                url = f"https://date.nager.at/api/v3/PublicHolidays/{today.year}/{country}"
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    dates = set()
                    for h in r.json():
                        dates.add(h["date"])
                    holidays[country] = dates
                else:
                    holidays[country] = set()
        except Exception as e:
            print(f"   API holiday fetch error: {e}", flush=True)
            holidays = cache.get("holidays", {})

    # Save to cache
    try:
        HOLIDAY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        HOLIDAY_CACHE_PATH.write_text(json.dumps({"ts": now, "holidays": holidays}))
    except:
        pass

    return holidays


def is_market_open(code, holidays):
    """Check if a market is currently open based on time + holidays.
    Returns True if the market for this code should be trading.
    """
    from datetime import timezone, timedelta

    utc_now = datetime.now(timezone.utc)
    hkt = utc_now.astimezone(timezone(timedelta(hours=8)))
    et = utc_now.astimezone(timezone(timedelta(hours=-4)))  # EDT

    weekday = hkt.weekday()  # 0=Mon, 6=Sun
    if weekday >= 5:  # Weekend
        return False

    today_str = hkt.strftime("%Y-%m-%d")

    if code.endswith(".SH") or code.endswith(".SZ"):
        # A-share: Mon-Fri 9:30-15:00 CST (UTC+8), check holiday
        if today_str in holidays.get("CN", set()):
            return False
        hkt_time = hkt.time()
        return hkt_time >= _datetime_time(9, 30) and hkt_time < _datetime_time(15, 0)

    elif code.endswith(".HK"):
        # HK: Mon-Fri 9:30-16:00 HKT, check holiday
        if today_str in holidays.get("HK", set()):
            return False
        hkt_time = hkt.time()
        return hkt_time >= _datetime_time(9, 30) and hkt_time < _datetime_time(16, 0)

    elif code.endswith(".O"):
        # US: Mon-Fri 9:30-16:00 ET, check holiday
        et_today = et.strftime("%Y-%m-%d")
        if et_today in holidays.get("US", set()):
            return False
        et_time = et.time()
        return et_time >= _datetime_time(9, 30) and et_time < _datetime_time(16, 0)

    return True  # Unknown market — allow

# === Volatility Context ===
def get_volatility_context(code):
    """Get volatility context for a stock code.
    Returns (formatted_string, z_score_value) tuple.
    z_score_value is None if no volatility data available.
    """
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
            f"Z-Score: {z} [{label}]",
            z
        )
    return "", None

# === Price Attribution ===
def _get_attribution_text(ticker, name, move_pct, market):
    """Fetch price attribution and return formatted text (or None)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "price_attribution",
            Path.home() / ".hermes" / "scripts" / "price_attribution.py"
        )
        if not spec or not spec.loader:
            log(f"  [Attribution] Failed to load price_attribution.py module")
            return None
        pa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pa)

        # US: Finnhub needs bare ticker (MU not MU.O)
        # HK/A: MarketAux needs full symbol (6869.HK not 6869)
        if market == "US":
            ticker_clean = ticker.split(".")[0]
        else:
            ticker_clean = ticker
        log(f"  [Attribution] Fetching for {ticker_clean} ({name}) {move_pct:+.1f}%")
        data = pa.fetch_attribution(ticker_clean, name, move_pct, market)
        articles = data.get("articles", [])
        log(f"  [Attribution] Found {len(articles)} articles")
        if not articles:
            log(f"  [Attribution] No news articles found - skipping attribution")
            return None

        analysis = pa.llm_attribution(
            articles[:5], ticker_clean, name, move_pct,
            analyst_consensus=data.get("analyst_consensus"),
            earnings_proximity=data.get("earnings_proximity", []),
            volume_context=data.get("volume_context"),
        )
        if not analysis:
            log(f"  [Attribution] LLM analysis returned None - skipping attribution")
            return None

        sources = list(set(a["source"] for a in articles[:5]))
        urls = [a["url"] for a in articles[:3] if a.get("url")]
        source_str = ", ".join(sources[:3]) if sources else "未知"
        attr_msg = f"\n\n价格归因:\n{analysis}\n来源：{source_str}"
        if urls:
            for u in urls[:2]:
                attr_msg += f"\n{u}"
        log(f"  [Attribution] Success - generated attribution text")
        return attr_msg
    except Exception as e:
        log(f"  [Attribution] Error: {e}\n{traceback.format_exc()}")
        return None

# === Sector Correlation ===
def check_sector_correlation(code, name, pct, direction, level=1):
    """Check if other stocks in same sector triggered alerts recently.
    If ≥2 Level 2+ alerts in same sector within 30min, flag as sector-wide extreme move.
    """
    global recent_alerts
    now = _time_module.time()

    # Clean old alerts
    recent_alerts = [a for a in recent_alerts if now - a["time"] < SECTOR_WINDOW]

    # Find sector for this stock
    stock_sector = None
    for sector, stocks in SECTOR_MAP.items():
        if code in stocks:
            stock_sector = sector
            break

    if not stock_sector:
        return

    # Add this alert with level info
    recent_alerts.append({
        "code": code, "name": name, "pct": pct,
        "direction": direction, "time": now, "sector": stock_sector, "level": level
    })

    # Check if >=2 stocks in same sector within window
    sector_alerts = [a for a in recent_alerts if a["sector"] == stock_sector]
    if len(sector_alerts) >= 2:
        # Check for sector-wide extreme move (>=2 Level 2+ alerts)
        high_level_count = sum(1 for a in sector_alerts if a.get("level", 1) >= 2)
        extreme_move = high_level_count >= 2

        directions = [a["direction"] for a in sector_alerts]
        if len(set(directions)) == 1:
            trend = "同向上涨" if "📈" in directions[0] else "同向下跌"
            judgment = "板块级别利好" if "📈" in directions[0] else "板块级别利空"
        else:
            trend = "板块分化"
            judgment = "板块内个股走势分化"

        stocks_str = " | ".join(
            f"{a['code']} L{a.get('level', 1)} {a['pct']*100:+.1f}%" for a in sector_alerts
        )

        extreme_tag = " ⚠️【极端异动】" if extreme_move else ""
        sector_msg = (
            f"\n\n 🚨 板块联动预警 — {stock_sector}{extreme_tag}\n"
            f"{stocks_str}\n"
            f"方向: {trend} ({len(sector_alerts)}只持仓)\n"
            f"判断: {judgment}"
        )
        send_discord(sector_msg)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)

def load_discord_token():
    try:
        with open(DISCORD_TOKEN_PATH) as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except: pass
    return None

def load_progressive_state():
    """Load progressive threshold tracking state. Resets daily."""
    global progressive_state
    today = datetime.now().strftime("%Y-%m-%d")
    if not PROGRESSIVE_STATE_PATH.exists():
        progressive_state = {}
        return
    try:
        data = json.loads(PROGRESSIVE_STATE_PATH.read_text())
        if data.get("date") != today:
            progressive_state = {}
            return
        progressive_state = data.get("stocks", {})
    except:
        progressive_state = {}


def save_progressive_state():
    """Save progressive threshold tracking state to disk."""
    today = datetime.now().strftime("%Y-%m-%d")
    data = {
        "date": today,
        "stocks": progressive_state
    }
    PROGRESSIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESSIVE_STATE_PATH.write_text(json.dumps(data, indent=2))


def get_current_level(pct, direction):
    """Get the highest threshold level reached for a given % move."""
    abs_pct = abs(pct)
    for i in range(len(PROGRESSIVE_LEVELS) - 1, -1, -1):
        if abs_pct >= PROGRESSIVE_LEVELS[i]:
            return i + 1
    return 0


def should_alert_progressive(code, pct, direction, threshold=ALERT_THRESHOLD):
    """Check if we should alert — with time-decay filter requiring N consecutive ticks above threshold.

    Args:
        code: Stock code
        pct: Price change as decimal (e.g. 0.07 for 7%)
        direction: "up" or "down"
        threshold: Base threshold for this stock (position-weighted)

    Returns:
        (should_alert, current_level) tuple
    """
    dir_key = "last_level_up" if direction == "up" else "last_level_down"
    counter_key = "consecutive_up" if direction == "up" else "consecutive_down"
    current_level = get_current_level(pct, direction)

    if code not in progressive_state:
        progressive_state[code] = {
            "last_level_up": 0,
            "last_level_down": 0,
            "consecutive_up": 0,
            "consecutive_down": 0,
            "last_time_up": None,
            "last_time_down": None,
        }

    # Migration: ensure all state fields exist
    for f in ("consecutive_up", "consecutive_down"):
        if f not in progressive_state[code]:
            progressive_state[code][f] = 0

    # Time-decay filter: check if this tick is strong enough to count
    abs_pct = abs(pct)
    if abs_pct >= threshold:
        progressive_state[code][counter_key] += 1
    else:
        # Reset consecutive counter when below threshold
        progressive_state[code][counter_key] = 0

    last_level = progressive_state[code][dir_key]
    consecutive = progressive_state[code][counter_key]

    # Only alert when: new level + consecutive requirement met
    should_alert = (
        current_level > last_level
        and current_level >= 1
        and consecutive >= TIME_DECAY_CONSECUTIVE
    )
    return should_alert, current_level


def send_discord(msg, mention_here=False):
    token = load_discord_token()
    if not token:
        log("  No Discord token found, skipping alert.")
        return
    if mention_here:
        msg = "@here " + msg
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages"
    try:
        r = requests.post(url, headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                      json={"content": msg}, timeout=10)
        if r.status_code == 200:
            log(f"  Alert sent: {msg[:60]}...")
        else:
            log(f"  Discord API error: {r.status_code}")
    except Exception as e:
        log(f"  Discord network error: {e}")

# === Sina Data Fetching ===

def parse_sina_line(line):
    """Parse a single line from Sina response and update state."""
    try:
        parts = line.split('="')
        if len(parts) < 2: return

        raw_symbol = parts[0].split('hq_str_')[-1]
        content = parts[1].strip('";')
        if not content: return

        fields = content.split(',')
        if len(fields) < 4: return

        if raw_symbol.startswith('rt_hk') or raw_symbol.startswith('hk'):
            name = fields[1]
            price = float(fields[6])  # rt_hk: price at index 6
            prev_close = price / (1 + float(fields[8]) / 100)  # Derive prev from fields[8] pct
        elif raw_symbol.startswith('gb_'):
            name_map = {"gb_lite": "LUMENTUM"}
            name = name_map.get(raw_symbol, fields[0])
            price = float(fields[1])
            prev_close = float(fields[26])
        else:
            name = fields[0]
            price = float(fields[3])
            prev_close = float(fields[2])

        if price <= 0 or prev_close <= 0: return

        with state_lock:
            state[raw_symbol] = {
                "price": price,
                "prev_close": prev_close,
                "name": name
            }
    except Exception:
        pass

def init_state():
    """Initialize market data via Sina."""
    log("   Initializing market data via Sina...")

    url = f"http://hq.sinajs.cn/list={','.join(SINA_ALL_SYMBOLS)}"
    headers = {"Referer": "https://finance.sina.com.cn"}

    try:
        r = session.get(url, headers=headers, timeout=10)
        r.encoding = 'gbk'

        count = 0
        for line in r.text.split('\n'):
            if line.strip():
                parse_sina_line(line)
                sym = line.split('hq_str_')[-1].split('="')[0]
                if sym in state: count += 1

        log(f"   Initialized {count} stocks via Sina")
    except Exception as e:
        log(f"   Sina initialization failed: {e}")

def polling_loop():
    """Main polling loop — Sina only, 3s interval."""
    global session
    log("Starting polling loop (Sina, 3s interval)...")
    sina_url = f"http://hq.sinajs.cn/list={','.join(SINA_ALL_SYMBOLS)}"
    sina_headers = {"Referer": "https://finance.sina.com.cn"}

    tick_count = 0
    while True:
        try:
            r = session.get(sina_url, headers=sina_headers, timeout=10)
            r.encoding = 'gbk'

            for line in r.text.split('\n'):
                if line.strip():
                    parse_sina_line(line)

            check_alerts()
        except Exception as e:
            log(f"  Polling error: {e}")

        tick_count += 1
        # Reset session every 1200 ticks (~1 hour) to prevent connection pool exhaustion
        if tick_count % 1200 == 0:
            log("  Periodic session reset (connection pool cleanup)")
            session = requests.Session()
            session.trust_env = False

        _time_module.sleep(3)

def sina_to_code(sina_symbol):
    """Convert Sina symbol format to portfolio code format.

    Also handles portfolio-formatted symbols directly.
    """
    # If already in portfolio format, return as-is
    if sina_symbol.endswith((".SH", ".SZ", ".HK", ".O", ".N", ".GI")):
        return sina_symbol

    if sina_symbol.startswith('gb_'):
        return sina_symbol.replace('gb_', '').upper() + '.O'
    elif sina_symbol.startswith('rt_hk') or sina_symbol.startswith('hk'):
        prefix = 'rt_hk' if sina_symbol.startswith('rt_hk') else 'hk'
        return sina_symbol.replace(prefix, '').lstrip('0') + '.HK'
    else:
        # A-share: sh600176 -> 600176.SH, sz002028 -> 002028.SZ
        if sina_symbol.startswith('sh'):
            return sina_symbol.replace('sh', '') + '.SH'
        elif sina_symbol.startswith('sz'):
            return sina_symbol.replace('sz', '') + '.SZ'
    return sina_symbol

def check_alerts():
    """Check thresholds with progressive levels and send alerts in Simplified Chinese.
    Levels: 7% → 12% → 18% → 25%+
    Only checks stocks when their market is actually open.
    Only alerts on PORTFOLIO holdings.
    Z-Score filter: only alert if |Z-Score| >= 1.0 OR no volatility data available.
    """
    with state_lock:
        current_state = dict(state)

    # Check which markets are open (cached hourly)
    holidays = get_market_holidays()

    # Build portfolio symbol set (only alert on actual holdings)
    portfolio_codes = set(position_weights.keys())

    triggered = 0
    for sym, info in current_state.items():
        price = info["price"]
        prev = info["prev_close"]
        if prev > 0 and price > 0:
            pct = (price - prev) / prev
            code = sina_to_code(sym)

            # Only alert on actual portfolio holdings
            if code not in portfolio_codes:
                continue

            # Skip if market is closed (holiday or after-hours)
            if not is_market_open(code, holidays):
                continue

            threshold = get_threshold(code)

            # Check progressive thresholds
            direction_str = "up" if pct > 0 else "down"
            should_alert, current_level = should_alert_progressive(code, pct, direction_str, threshold)

            if should_alert:
                # Z-Score filter: only alert if |Z-Score| >= 1.0 OR no volatility data
                vol_context, z_score = get_volatility_context(code)
                if z_score is not None:
                    try:
                        if abs(float(z_score)) < 1.0:
                            # Volatility is normal — skip alert
                            # But still update progressive state
                            dir_key = "last_level_up" if pct > 0 else "last_level_down"
                            time_key = "last_time_up" if pct > 0 else "last_time_down"
                            progressive_state[code][dir_key] = current_level
                            progressive_state[code][time_key] = datetime.now().isoformat()
                            save_progressive_state()
                            continue
                    except (ValueError, TypeError):
                        pass  # If z_score is not a number, proceed with alert

                # Update progressive state
                dir_key = "last_level_up" if pct > 0 else "last_level_down"
                time_key = "last_time_up" if pct > 0 else "last_time_down"
                progressive_state[code][dir_key] = current_level
                progressive_state[code][time_key] = datetime.now().isoformat()
                save_progressive_state()

                direction = "📈" if pct > 0 else "📉"
                name = info.get('name', code)
                level_name = PROGRESSIVE_LEVEL_NAMES[current_level - 1]

                # Calculate next threshold
                next_thresh = PROGRESSIVE_LEVELS[current_level] if current_level < len(PROGRESSIVE_LEVELS) else None
                next_hint = f" → 下次预警: {next_thresh*100:.0f}%" if next_thresh else " → 已达最高级别"

                # Alert message
                msg = (
                    f"{direction} **[{level_name}] {code}** ({name}) 触发预警\\n"
                    f"现价：{price:.3f}\\n"
                    f"涨跌幅：{pct*100:+.2f}% (当前阈值：{threshold*100:.1f}%){next_hint}"
                )

                # Volatility context
                if vol_context:
                    msg += vol_context

                # Price attribution (inline)
                try:
                    if code.endswith(".O"):
                        t_ticker = code.replace(".O", "")
                        t_market = "US"
                    elif code.endswith(".HK"):
                        t_ticker = code
                        t_market = "HK"
                    elif code.endswith(".SH") or code.endswith(".SZ"):
                        t_ticker = code
                        t_market = "A"
                    else:
                        t_ticker = None
                    if t_ticker:
                        a_msg = _get_attribution_text(t_ticker, name, pct * 100, t_market)
                        if a_msg:
                            msg += a_msg
                except Exception as e:
                    print(f"   Attribution inline error: {e}", flush=True)

                send_discord(msg, mention_here=(current_level >= 3))
                triggered += 1

                # Sector correlation (with level info)
                check_sector_correlation(code, name, pct, direction, current_level)

    if triggered > 0:
        log(f"   Triggered {triggered} alerts!")


if __name__ == "__main__":
    log(" Portfolio Monitor v5.1 (Sina Polling + Thread Self-Heal)")
    log("=" * 50)

    # Load position weights
    load_position_weights()

    # Load volatility state
    load_volatility_state()

    init_state()

    # Load progressive threshold tracking state
    load_progressive_state()
    if progressive_state:
        active_alerts = sum(1 for s in progressive_state.values() if s["last_level_up"] > 0 or s["last_level_down"] > 0)
        log(f"   Loaded progressive state for {len(progressive_state)} stocks ({active_alerts} active alerts)")

    def start_polling_thread():
        """Start the polling thread, return the thread object."""
        t = threading.Thread(target=polling_loop, daemon=False)
        t.start()
        return t

    polling_thread = start_polling_thread()

    log("Polling thread started")

    try:
        while True:
            _time_module.sleep(10)
            # Check if polling thread is still alive — restart if crashed
            if not polling_thread.is_alive():
                log("  ⚠️ Polling thread died unexpectedly — restarting...")
                polling_thread = start_polling_thread()
                log("  ✅ Polling thread restarted")
    except KeyboardInterrupt:
        log("\nStopped by user")
