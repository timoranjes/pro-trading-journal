#!/usr/bin/env python3
"""
Portfolio Price Alert System - v2 (Email-based):
1. Fetches "Daily Report (Ma Teng)" text email via himalaya
2. Parses tab-separated portfolio table (code, name, weight)
3. Fetches live prices from Sina Finance
4. Checks for 7%+ changes vs previous day
5. Outputs alerts for Discord delivery
"""
import subprocess
import json
import re
from datetime import datetime
from pathlib import Path

sys_path = __import__('sys')
sys_path.stdout.reconfigure(line_buffering=True)

STATE_FILE = Path.home() / ".hermes" / "portfolio_state.json"
from portfolio_config import SECTOR_MAP, send_discord, DISCORD_CHANNEL, WEBHOOK_CACHE_PATH, ALERT_THRESHOLD
from price_attribution import fetch_attribution, llm_attribution, fetch_market_context
import requests


def get_market_type(code):
    """Determine market type from stock code suffix.
    
    Returns: 'US' for .O, 'HK' for .HK, 'A' for .SH/.SZ
    """
    if code.endswith(".O"):
        return "US"
    elif code.endswith(".HK"):
        return "HK"
    elif code.endswith(".SH") or code.endswith(".SZ"):
        return "A"
    return "US"  # default


def get_attribution_ticker(code, market):
    """Format ticker for the attribution API.
    
    US: strip .O suffix (Finnhub expects bare ticker like 'MU')
    HK/A: keep full code (MarketAux accepts '6869.HK', '002353.SZ')
    """
    if market == "US":
        return code.replace(".O", "")
    return code


def fetch_rumor_context(code):
    """Fetch guba rumor context for an A-share from the rumor monitor state.
    
    Returns a dict with 'attention', 'rank_change', 'hot_posts' if available,
    or None if no rumor data exists for this stock.
    """
    if not (code.endswith(".SZ") or code.endswith(".SH")):
        return None
    
    GUBA_STATE = Path.home() / ".hermes/cron/output/guba_rumor_state.json"
    if not GUBA_STATE.exists():
        return None
    
    try:
        with open(GUBA_STATE) as f:
            state = json.load(f)
        
        att_data = state.get("attention", {}).get(code, {})
        if not att_data:
            return None
        
        # Get last run time to check recency
        last_run = state.get("last_run", "")
        
        result = {
            "attention": att_data.get("attention", 0),
            "rank_change": att_data.get("rank_change", 0),
            "rank": att_data.get("rank", 0),
            "last_updated": last_run,
        }
        
        # If there's scraped post data from the cron, include top posts
        posts = state.get("posts", {}).get(code, [])
        if posts:
            result["hot_posts"] = posts[:3]  # Top 3
        
        return result
        
    except Exception as e:
        print(f"  [rumor] {code}: state read error — {e}")
        return None


def format_rumor_attribution(code, name, change_pct, rumor_data):
    """Format rumor context for inclusion in price alert attribution."""
    if not rumor_data:
        return None, []
    
    lines = []
    sources = []
    att = rumor_data.get("attention", 0)
    rank_change = rumor_data.get("rank_change", 0)
    rank = rumor_data.get("rank", 0)
    
    # Attention signal
    if att > 90:
        lines.append(f"股吧关注度极高 (关注指数 {att:.0f}, 全市场排名 #{rank})")
    elif att > 85:
        lines.append(f"股吧关注度高 (关注指数 {att:.0f}, 排名 #{rank})")
    
    # Rank change signal
    if rank_change > 50:
        lines.append(f"关注度飙升 (排名上升 {rank_change:+d})")
    
    # Hot posts
    posts = rumor_data.get("hot_posts", [])
    if posts:
        for post in posts:
            title = post.get("title", "")
            reads = post.get("reads", 0)
            comments = post.get("comments", 0)
            if reads > 500 or comments > 10:
                lines.append(f"热帖: {title} (阅读{reads} 评论{comments})")
                sources.append(f"股吧热帖: {title}")
    
    if lines:
        return " | ".join(lines), sources
    return None, []


def fetch_alert_attribution(code, name, change_pct, sina_fields=None, market_context=None):
    """Fetch attribution for an alerted stock.
    
    Returns dict with 'analysis' (LLM text), 'sources' (list), and 'rumor' (rumor context),
    or None on any failure. Never raises — all errors are caught and logged.
    """
    try:
        market = get_market_type(code)
        ticker = get_attribution_ticker(code, market)
        
        data = fetch_attribution(ticker, name, change_pct, market, sina_fields=sina_fields)
        
        if not data.get("articles"):
            print(f"  [attribution] {code}: no articles found, skipping LLM")
            return None
        
        # Use passed market_context or fall back to data from fetch_attribution
        ctx = market_context or data.get("market_context")
        attribution_text = llm_attribution(
            data["articles"], ticker, name, change_pct,
            analyst_consensus=data.get("analyst_consensus"),
            earnings_proximity=data.get("earnings_proximity"),
            volume_context=data.get("volume_context"),
            market_context=ctx,
            technical_indicators=data.get("technical_indicators"),
        )
        
        # Build source list for manual verification
        sources = []
        for a in data["articles"][:3]:
            title = a.get("headline", "")[:60]
            url = a.get("url", "")
            src = a.get("source", "")
            if url:
                sources.append(f"[{src}] {title} — {url}")
            elif src and title:
                sources.append(f"[{src}] {title}")
        
        # Fetch rumor context for A-shares
        rumor_text = None
        rumor_sources = []
        if market == "A":
            rumor_data = fetch_rumor_context(code)
            if rumor_data:
                rumor_text, rumor_sources = format_rumor_attribution(
                    code, name, change_pct, rumor_data
                )
                print(f"  [rumor] {code}: {rumor_text[:80] if rumor_text else 'no significant rumors'}")
        
        result = {"sources": sources}
        if rumor_text:
            result["rumor"] = rumor_text
            result["sources"].extend(rumor_sources)
        
        # Merge LLM dict fields (headline, detail, confidence, etc.) directly
        if isinstance(attribution_text, dict):
            result.update(attribution_text)
        elif attribution_text:
            result["analysis"] = attribution_text
        
        if attribution_text:
            preview = attribution_text if isinstance(attribution_text, str) else attribution_text.get("headline", "")
            print(f"  [attribution] {code}: {str(preview)[:80]}...")
        return result
        
    except Exception as e:
        print(f"  [attribution] {code}: error — {e}")
        return None


def fetch_daily_report_email():
    """Fetch the latest Daily Report (Ma Teng) email text."""
    print("Fetching email...")

    result = subprocess.run(
        ["himalaya", "envelope", "list", "--page-size", "50", "--output", "json"],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        print(f"Email list error: {result.stderr}")
        return None

    emails = json.loads(result.stdout) if result.stdout.strip() else []

    # Find "Daily Report (Ma Teng)"
    report_id = None
    for email in emails:
        subj = email.get("subject", "")
        if "Daily Report" in subj and "Ma Teng" in subj:
            report_id = email["id"]
            break

    if not report_id:
        print("No Daily Report (Ma Teng) found")
        return None

    # Read email body
    result = subprocess.run(
        ["himalaya", "message", "read", str(report_id)],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        print(f"Email read error: {result.stderr}")
        return None

    return result.stdout


def parse_portfolio_from_email(email_text):
    """Parse tab-separated portfolio table from email.
    
    Format:
    No.\t股票代码\t股票名称\t仓位
    1\tSNDK.O\t闪迪\t10.90%
    """
    print("Parsing portfolio from email...")

    lines = email_text.split('\n')
    stocks = []

    # Pattern: number \t CODE \t Name \t Weight% (may have additional columns after)
    row_pattern = re.compile(r'^\d+\t([A-Z0-9]+\.[A-Z]{1,2})\t(.+?)\t([\d.]+)%')

    for line in lines:
        line = line.strip()
        match = row_pattern.match(line)
        if match:
            code = match.group(1)
            name = match.group(2).strip()
            weight = float(match.group(3)) / 100.0
            stocks.append({
                'code': code,
                'name': name,
                'weight': weight,
            })

    print(f"Parsed {len(stocks)} positions from email")
    return stocks


def is_market_open(code):
    """Check if the market for a given stock code is currently open.
    
    US (.O): Mon-Fri 9:30-16:00 ET → 21:30-04:00 HKT (EDT) / 22:30-05:00 HKT (EST)
    HK (.HK): Mon-Fri 9:30-12:00, 13:00-16:00 HKT
    A (.SH/.SZ): Mon-Fri 9:30-11:30, 13:00-15:00 CST (09:30-11:30, 13:00-15:00 HKT)
    """
    now_utc = datetime.now(__import__('datetime').timezone.utc)
    now_et = now_utc - __import__('datetime').timedelta(hours=4)  # Approximate ET (EDT)
    
    market = get_market_type(code)
    
    if market == "US":
        # US market: Mon-Fri 9:30-16:00 ET
        et_hour = now_et.hour + now_et.minute / 60.0
        weekday = now_et.weekday()  # 0=Mon, 6=Sun
        if weekday >= 5:  # Weekend
            return False
        if 9.5 <= et_hour < 16.0:
            return True
        return False
    elif market == "HK":
        # HK market: Mon-Fri 9:30-12:00, 13:00-16:00 HKT
        now_hkt = now_utc + __import__('datetime').timedelta(hours=8)
        weekday = now_hkt.weekday()
        if weekday >= 5:
            return False
        hkt_hour = now_hkt.hour + now_hkt.minute / 60.0
        if (9.5 <= hkt_hour < 12.0) or (13.0 <= hkt_hour < 16.0):
            return True
        return False
    elif market == "A":
        # A-share: Mon-Fri 9:30-11:30, 13:00-15:00 CST (=HKT)
        now_hkt = now_utc + __import__('datetime').timedelta(hours=8)
        weekday = now_hkt.weekday()
        if weekday >= 5:
            return False
        hkt_hour = now_hkt.hour + now_hkt.minute / 60.0
        if (9.5 <= hkt_hour < 11.5) or (13.0 <= hkt_hour < 15.0):
            return True
        return False
    return True


def fetch_live_prices(portfolio_codes):
    """Fetch live prices from Sina Finance for all portfolio stocks.
    
    Skips stocks whose markets are currently closed to avoid stale data.
    """
    sina_symbols = []
    code_to_sina = {}
    skipped = []

    for code in portfolio_codes:
        if not is_market_open(code):
            market = get_market_type(code)
            skipped.append(f"{code} ({market} market closed)")
            continue
        if code.endswith(".O"):
            sym = f"gb_{code.replace('.O', '').lower()}"
        elif code.endswith(".HK"):
            sym = f"rt_hk{code.replace('.HK', '').zfill(5)}"  # rt_ prefix for real-time HK data
        elif code.endswith(".SH"):
            sym = f"sh{code.replace('.SH', '')}"
        elif code.endswith(".SZ"):
            sym = f"sz{code.replace('.SZ', '')}"
        else:
            continue
        sina_symbols.append(sym)
        code_to_sina[code] = sym

    if not sina_symbols:
        return {}

    url = f"http://hq.sinajs.cn/list={','.join(sina_symbols)}"
    headers = {"Referer": "https://finance.sina.com.cn"}

    import requests
    live_prices = {}

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
            if len(fields) < 4:
                continue

            try:
                if raw_sym.startswith('rt_hk') or raw_sym.startswith('hk'):
                    price = float(fields[6])  # rt_hk: price at index 6
                    pct = float(fields[8])
                    prev_close = price / (1 + pct / 100)
                elif raw_sym.startswith('gb_'):
                    price = float(fields[1])
                    prev_close = float(fields[26])
                else:
                    price = float(fields[3])
                    prev_close = float(fields[2])

                if price <= 0 or prev_close <= 0:
                    continue

                pct_change = (price - prev_close) / prev_close
                # Convert sina symbol back to portfolio code
                code = None
                for c, s in code_to_sina.items():
                    if s == raw_sym:
                        code = c
                        break

                if code:
                    live_prices[code] = {
                        'price': price,
                        'prev_close': prev_close,
                        'pct': pct_change,
                        'sina_fields': fields,  # Keep raw fields for volume context
                        'fetched_at': datetime.now().isoformat(),
                    }
            except (ValueError, IndexError):
                continue
    except Exception as e:
        print(f"Sina fetch error: {e}")

    if skipped:
        print(f"  [skip] {len(skipped)} markets closed: {', '.join(skipped[:5])}")
        live_prices['_skipped'] = skipped

    return live_prices


def check_alerts(stocks, live_prices, prev_state, market_context=None):
    """Check for 7%+ day-over-day price changes using direct API data."""
    alerts = []
    for s in stocks:
        code = s['code']
        live = live_prices.get(code)
        if not live:
            continue

        # Use the exchange's official daily change directly — no state dependency
        change = live['pct']

        if abs(change) >= ALERT_THRESHOLD:
            alert = {
                'code': code,
                'name': s['name'],
                'prev_price': live['prev_close'],
                'curr_price': live['price'],
                'change': change * 100,
                'dir': "📈" if change > 0 else "📉",
                'weight': s.get('weight'),
                'fetched_at': live.get('fetched_at'),
                'attribution': None,
            }
            # Fetch attribution (non-blocking — failure = None)
            print(f"  [attribution] Fetching for {code} ({s['name']}) {change*100:+.1f}%...")
            alert['attribution'] = fetch_alert_attribution(
                code, s['name'], change * 100,
                sina_fields=live.get('sina_fields'),
                market_context=market_context,
            )
            alerts.append(alert)

    return alerts


def save_state(stocks, live_prices):
    """Save current portfolio state with price, name, weight."""
    state = {}
    for s in stocks:
        code = s['code']
        live = live_prices.get(code)
        entry = {
            'price': live['price'] if live else 0,
            'name': s['name'],
        }
        if s.get('weight') is not None:
            entry['weight'] = s['weight']
        state[code] = entry

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    print(f"Saved state: {len(state)} positions")


def main(send_webhook=True):
    print(f"=== Portfolio Alert @ {datetime.now().isoformat()} ===")

    # Step 1: Fetch email
    email_text = fetch_daily_report_email()
    if not email_text:
        print("No email available")
        return

    # Step 2: Parse portfolio from email
    stocks = parse_portfolio_from_email(email_text)
    if not stocks:
        print("No positions found in email")
        return

    # Step 3: Fetch live prices from Sina
    codes = [s['code'] for s in stocks]
    print(f"Fetching live prices: {len(codes)} stocks")
    live_prices = fetch_live_prices(codes)
    print(f"Got live prices: {len(live_prices)} stocks")
    if live_prices.get('_skipped'):
        for skip_msg in live_prices.pop('_skipped'):
            print(f"  [skip] {skip_msg}")

    # Step 4: Load previous state
    prev_state = {}
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            prev_state = json.load(f)

    # Step 4b: Fetch market context once per run (broad market backdrop)
    market_context = fetch_market_context()
    if market_context:
        parts = []
        for idx, pct in market_context.items():
            sign = "+" if pct >= 0 else ""
            parts.append(f"{idx}: {sign}{pct}%")
        print(f"  [market] {', '.join(parts)}")

    # Step 5: Check alerts (with market context for LLM attribution)
    alerts = check_alerts(stocks, live_prices, prev_state, market_context=market_context)

    # Step 6: Save current state
    save_state(stocks, live_prices)

    # Step 7: Output alerts — send EACH one individually (PM-ready format)
    if alerts:
        for a in alerts:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M HKT")
            
            # Portfolio impact
            weight_str = f"{a['weight']*100:.0f}%" if a.get('weight') else "N/A"
            try:
                contrib = a['weight'] * a['change']
                contrib_str = f"{contrib:+.2f}% (仓位权重 {weight_str})"
            except:
                contrib_str = f"权重 {weight_str}"
            
            arrow = "▲" if a['change'] > 0 else "▼"
            sign = "+" if a['change'] > 0 else ""
            
            lines = []
            lines.append("━━━ 价格异动报告 ━━━")
            lines.append(f"时间: {now_str}")
            lines.append(f"标的: {a['code']} {a['name']}")
            lines.append(f"前收→现价: {a['prev_price']:.2f} → {a['curr_price']:.2f}")
            lines.append(f"涨跌幅: {sign}{a['change']:.2f}%")
            lines.append(f"组合影响: {contrib_str}")
            
            # Attribution — detailed analysis
            if a.get('attribution'):
                attr = a['attribution']
                if isinstance(attr, dict):
                    # Rumor context (shown FIRST — pre-news signal)
                    if attr.get('rumor'):
                        lines.append("")
                        lines.append("─── 散户情绪 / 传闻信号 ───")
                        lines.append(f"情绪: {attr['rumor']}")
                    
                    headline = attr.get('headline', '')
                    detail = attr.get('detail', '')
                    confidence = attr.get('confidence', '')
                    source_title = attr.get('source_title', '')
                    source_url = attr.get('source_url', '')
                    
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
                    elif attr.get('catalyst'):
                        lines.append("")
                        lines.append("─── 驱动因素分析 ───")
                        for line in attr['catalyst'].strip().split('\n'):
                            if line.strip():
                                lines.append(f"分析: {line.strip()}")
                        if attr.get('confidence'):
                            lines.append(f"可信度评估: {attr['confidence']}")
                    elif attr.get('analysis'):
                        lines.append("")
                        lines.append("─── 驱动因素分析 ───")
                        for line in attr['analysis'].strip().split('\n'):
                            if line.strip():
                                lines.append(f"分析: {line.strip()}")
                    for src in attr.get('sources', []):
                        lines.append(f"来源: {src}")
                else:
                    lines.append("")
                    lines.append("─── 驱动因素分析 ───")
                    for line in attr.strip().split('\n'):
                        if line.strip():
                            lines.append(f"分析: {line.strip()}")
            
            lines.append("")
            lines.append("━━━ End ━━━")
            
            msg = "\n".join(lines)
            print(msg)
            if send_webhook:
                send_discord(msg)
            else:
                print("  [no-webhook] Message not sent")
    else:
        print(f"✅ {len(stocks)} positions, no 7%+ changes")


import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-webhook", action="store_true",
                        help="Print alerts without sending Discord webhook")
    args = parser.parse_args()
    main(send_webhook=not args.no_webhook)
