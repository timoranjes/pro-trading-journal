#!/usr/bin/env python3
"""
Portfolio Alert System — Tier 4 News & Events Monitor (v4):
Self-configuring: auto-discovers competitors, maps commodities to holdings.

Detection layers (zero LLM cost):
  1. Per-stock news (A-share via AKShare)
  2. SEC 8-K filings (US stocks via EDGAR RSS)
  3. Earnings surprises (Finnhub calendar, >5% beat/miss)
  4. A-share announcements (filtered for material events)
  5. Competitor earnings & news (auto-discovered via Finnhub peers)
  6. Commodity prices (auto-mapped to holdings by industry keywords)
  7. Macro events (rate decisions, export controls, tariff news)
  8. Portfolio composition (concentration warnings, daily 08:00-12:00 only)
  9. Cross-source news (Marketaux + Finnhub market-news)
  10. HK/A-Share Earnings Calendar
  11. Geopolitical Risk (RSS feeds + Finnhub)
  13. Finnhub US stock news fallback (per-stock company-news)
  14. HKEX announcements (share buybacks, substantial shareholdings, director changes)
  15. Central Bank Speeches (Fed, ECB, PBOC — unscheduled remarks)
  16. SEC Form 4 Insider Trading (director/officer buys & sells)
  17. Tier-1 Financial Press (Bloomberg, WSJ, FT headlines)

Features:
  - event_type classification (corporate_action, earnings, regulatory, etc.)
  - Discord webhook delivery (send_to_discord)
  - Tuned noise/material patterns for better signal-to-noise

All material events written to pending_news.json for LLM analysis + Discord delivery.
"""

import sys
import os
import json
import re
import time
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests
import feedparser

try:
    from scrapling import StealthyFetcher
    SCRAPLING_AVAILABLE = True
except ImportError:
    SCRAPLING_AVAILABLE = False

sys.stdout.reconfigure(line_buffering=True)

# --- Configuration ---
FINNHUB_API_KEY = "d6n74gpr01qir35jdoagd6n74gpr01qir35jdob0"
# Marketaux API key (free tier: 100 req/day). Get free key at https://www.marketaux.com/
MARKETAUX_API_KEY = "I5ceElzzr8fOizcswE6aH9IclGgBVcI4GQ3dNnmh"
STATE_DIR = Path.home() / ".hermes" / "data"
NEWS_HISTORY_PATH = STATE_DIR / "news_history.json"
PENDING_NEWS_PATH = STATE_DIR / "pending_news.json"
PERF_LOG_PATH = STATE_DIR / "news_monitor_perf.json"
GLOBAL_TIMEOUT = 180  # seconds — extended to allow all 17 layers to complete
PORTFOLIO_STATE_PATH = Path.home() / ".hermes" / "portfolio_state.json"
COMPETITOR_CACHE_PATH = STATE_DIR / "competitor_cache.json"
# --- SEC CIK Cache ---
SEC_CIK_CACHE_PATH = STATE_DIR / "sec_cik_cache.json"
SEC_CIK_UPDATE_INTERVAL = timedelta(days=1)  # Refresh daily

# --- HKEX Announcement Monitoring ---
HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
HKEX_STOCK_ID_CACHE_PATH = STATE_DIR / "hkex_stock_id_cache.json"
HKEX_STOCK_ID_CACHE_TTL = timedelta(days=7)  # Cache stock IDs for 7 days

# --- HK Stock Code List for AKShare ---
# AKShare stock_news_em works for HK stocks but 4-digit codes can match noise.
# These are the portfolio's HK stock codes for monitoring.
HK_STOCK_CODES = {
    '01888': '建滔积层板',
    '02259': '紫金黄金',
    '06869': '长飞光纤',
    '00709': '威胜控股',
    '00813': '佳鑫国际',
    '03808': '中国动力',
    '06830': '智谱',
    '00066': '港铁公司',
}

# --- Discord Webhook (loaded from cache) ---
WEBHOOK_CACHE_PATH = Path.home() / ".hermes" / "data" / "webhooks.json"
PORTFOLIO_ALERTS_CHANNEL_ID = "1502241038579011655"  # #portfolio-alerts (restored)

def _load_discord_token():
    """Load Discord bot token from .env file."""
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None

def get_portfolio_alerts_webhook():
    """Get the #portfolio-alerts webhook URL from cache, with auto-provision."""
    cache_path = WEBHOOK_CACHE_PATH
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            if PORTFOLIO_ALERTS_CHANNEL_ID in cache:
                return cache[PORTFOLIO_ALERTS_CHANNEL_ID]
        except:
            pass
    # Fallback: try to auto-provision via webhook_manager
    token = _load_discord_token()
    if not token:
        return None
    try:
        import subprocess
        result = subprocess.run(
            ["python3", str(Path(__file__).parent / "webhook_manager.py"),
             "get", "portfolio-alerts"],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.splitlines():
            if line.startswith("URL:"):
                return line.split(":", 1)[1].strip()
    except:
        pass
    return None

# --- Materiality Filters ---
NOISE_PATTERNS = [
    # Pure price/volume noise (no fundamental info)
    r"成交额", r"资金流向", r"资金流出", r"资金流入",
    r"龙虎榜", r"巨资抢筹", r"垂直涨停",
    # Routine reports & compliance boilerplate
    r"内部控制评价", r"内部控制审计",
    r"募集资金存放", r"募集资金使用",
    r"独立董[事]*述职", r"社会责任报告", r"可持续发展报告",
    r"ESG公告", r"环境报告",
    # Generic analyst/research noise (but keep specific material ones)
    r"研报$", r"调研纪要$", r"投资者关系活动",
    # Removed overly aggressive patterns that were filtering real material events:
    #   - 涨停/跌停 (can signal material moves)
    #   - 主力 (too broad, catches "主力产品" etc.)
    #   - 外资持股/北向/南向 (capital flow signals)
    #   - 调研报告/路演 (sometimes carry material forward guidance)
    #   - 分析师/观点/解读/评论/分析 (way too broad — catches any headline)
    #   - 概念/板块走强走弱 (sector rotation is material)
    #   - 指数发布 (some index changes matter)
    #   - 获得专利/获奖 (sometimes material for biotech/IP-heavy firms)
    #   - 复盘/日报/周报 (still noisy but less aggressive)
    #   - 大爆发/利好/A股第一 (hype but sometimes real)
]

# --- Routine HKEX Filings (English) — always filter out, never material ---
ROUTINE_HKEX_PATTERNS = [
    r"Monthly Returns",           # Standard monthly portfolio disclosure (ETF/AMC)
    r"List of Shareholders",      # Routine shareholder list
    r"Settlement of Trading",     # Standard settlement info
    r"Notification of Director",  # Standard director notification (routine)
    r"Notice of Meeting",         # Standard meeting notice (without material agenda)
    r"Circular.*General",         # General circulars
    r"Annual Report",             # Standard annual report filing
    r"Interim Report",            # Standard interim report filing
    r"Proxy Form",                # Standard proxy form
]

MATERIAL_PATTERNS = [
    r"重大[合同|事项|事件|重组|诉讼|处罚|违规]",
    r"签订.*合同", r"签订.*协议", r"签.*大单",
    r"收购", r"并购", r"重组", r"合并", r"借壳",
    r"被.*调查", r"被.*立案", r"被.*处罚", r"被.*罚款",
    r"退市", r"停牌", r"复牌", r"摘牌",
    r"业绩超预期", r"业绩不及预期", r"超预期", r"不及预期",
    r"扭亏为盈", r"亏损扩大",
    r"大股东.*减持", r"大股东.*增持", r"控股股东.*变更",
    r"实际控制人.*变更", r"实控人.*变更",
    r"举牌", r"权益变动",
    r"董事长.*辞职", r"CEO.*辞职", r"总经理.*辞职",
    r"董秘.*辞职", r"高管.*被.*调查",
    r"定增", r"配股", r"增发", r"回购.*注销",
    r"破产", r"重整", r"清算",
    r"产品.*召回", r"生产线.*停产", r"工厂.*停产",
    r"获得.*批文", r"获得.*批准", r"获批",
    r"核心技术人员.*离职",
    # --- Expanded material patterns ---
    r"分红", r"派息", r"特别分红",
    r"可转债", r"转股.*价格.*修正", r"向下修正转股",
    r"质押.*平仓", r"股权质押.*预警", r"补充质押",
    r"减持计划", r"增持计划",
    r"业绩预告", r"业绩快报", r"业绩修正",
    r"商誉.*减值", r"资产减值", r"计提.*减值",
    r"行政处罚", r"监管.*函", r"警示函", r"责令改正",
    r"诉讼.*判决", r"仲裁.*裁决",
    r"债务.*违约", r"债券.*违约",
    r"产能.*扩张", r"新工厂", r"新产线", r"投产",
    r"签署.*战略合作", r"战略合作.*框架",
    r"国家.*科技进步", r"重点.*专项", r"国家.*补贴",
    r"关税.*豁免", r"关税.*加征", r"加征.*关税",
    r"反垄断", r"反倾销",
    r"停工", r"停产.*整改", r"环保.*处罚",
    r"重大.*安全事故", r"安全事故",
    r"减持.*预披露", r"大宗交易.*折价",
    r"要约收购", r"部分要约",
    r"退市.*风险", r"\*ST", r"ST",
    # --- HKEX-specific material patterns (English) ---
    r"Share Buyback", r"Buyback", r"Substantial Shareholder", r"Shareholding Change",
    r"Director Change", r"Company Secretary", r"Annual General Meeting",
    r"Dividend", r"Final Dividend", r"Interim Dividend",
    r"Results Announcement", r"Performance", r"Earnings",
    r"Major Acquisition", r"Major Disposal", r"Connected Transaction",
    r"Next Day Disclosure",
    r"Listed", r"Delisting", r"Withdrawal",
    r"Spin-off", r"Carve-out", r"Restructuring",
    r"Tender Offer", r"General Mandate",
    # --- English material patterns (L13 Finnhub US, L17 FinPress) ---
    r"[Aa]cquisition\b", r"[Mm]erger\b", r"[Aa]cquire[sd]?\b",
    r"[Rr]ecalls?\b", r"[Ff]raud\b", r"[Ii]nvestigat(ed|ion|s)?\b",
    r"[Ll]awsuit\b", r"[Cc]lass action", r"bankruptc[yies]\b",
    r"insolven[cy]\b", r"delist(ed|ing)?\b", r"suspend[ed]?\b",
    r"downgrad(ed|ing|e)?\b", r"upgrad(ed|ing|e)?\b",
    r"[Bb]uyback\b", r"[Bb]uy-back\b", r"[Rr]epurchase\b",
    r"[Dd]ividend", r"[Ss]tock split", r"[Oo]ffers?\b",
    r"profit warn(ing)?\b", r"miss(es|ed)?\b.*expect",
    r"beat.*expect", r"surpass.*expect",
    r"export (control|restriction|ban)", r"sanction(ed|s)?\b",
    r"tariff(s)?\b", r"trade restriction", r"entit(y|ies).*list",
    r"chip shortage", r"[Ss]upply chain.*disrupt", r"plant.*clos(ed|ing|ure)",
    r"factory.*halt", r"layoff(s)?\b", r"workforce reduction",
    r"CEO.*(resign|quit|step down)", r"executive.*(resign|leave|fired)",
    r"SEC.*(charge|probe|investigate)", r"antitrust", r"monopoly",
]

PROFIT_GROWTH_THRESHOLD = 20
PROFIT_DECLINE_THRESHOLD = -15

# --- Geopolitical & Macro Keyword Severity Tiers ---
# CRITICAL (1 match → fire), HIGH (2+ matches → fire), NORMAL (3+ matches → fire)
GEOPOLITICAL_TIERS = {
    'CRITICAL': [
        'war', 'attack', 'military', 'iran', 'hormuz', 'strait', 'sanction',
        'nuclear', 'escalation', 'blockade', 'missile', 'invade', 'invasion',
        'strike', 'munition', 'centcom', 'explosion', 'ceasefire', 'cease-fire',
        'ukraine', 'russia', 'gaza', 'hezbollah', 'houthi', 'red sea',
        '制裁', '袭击', '战争', '军事', '核', '导弹', '封锁', '冲突升级',
        '打击', '开战', '原油', '石油',
    ],
    'HIGH': [
        'conflict', 'crisis', 'tariff', 'trade war', 'embargo',
        'supply chain', 'inflation', 'recession', 'deflation',
        'interest rate', 'rate hike', 'rate cut', 'shutdown',
        '冲突', '危机', '关税', '贸易战', '供应链', '通胀', '利率',
        '衰退', '加息', '降息',
    ],
    'NORMAL': [
        'tension', 'diplomatic', 'protest', 'deployment', 'warning',
        '紧张', '外交', '抗议', '部署', '警告',
    ],
}

# --- Geopolitical Event → Portfolio Impact Mapping ---
GEOPOLITICAL_IMPACT_MAP = {
    'middle_east': {
        'keywords': ['iran', 'hormuz', 'uae', '中东', 'strait', 'oil facility',
                     'crude', 'red sea', 'houthi', 'gaza', 'hezbollah',
                     'lebanon', 'israel', 'tehran', 'dubai'],
        'sectors': ['能源/黄金', '整体市场'],
        'stocks': ['2259.HK', '002353.SZ'],
        'description': '中东地缘政治风险 → 原油、黄金、全球风险情绪',
    },
    'trade_war': {
        'keywords': ['tariff', 'trade war', '关税', '贸易战', 'export control',
                     'export restriction', 'sanction', '制裁', 'decoupling'],
        'sectors': ['半导体', '光通信/光模块', '整体市场'],
        'stocks': ['MU.O', 'SNDK.O', 'LITE.O', '300308.SZ', '300502.SZ',
                    '6869.HK', '3858.HK'],
        'description': '贸易战/关税/制裁 → 半导体、光模块、光纤光缆、金属资源',
    },
    'fed_policy': {
        'keywords': ['fed', 'federal reserve', 'interest rate', '加息', '降息',
                     'fomc', 'powell', '美联储', '利率决策'],
        'sectors': ['整体市场', '半导体', '新能源'],
        'stocks': ['MU.O', 'SNDK.O', 'LITE.O', '2513.HK', '300750.SZ', '601138.SH'],
        'description': '美联储政策 → 科技股估值、成长股、市场流动性',
    },
    'china_policy': {
        'keywords': ['china', '中国', 'beijing', '北京', 'xi jinping', '习近平',
                     'chinese', '中国人', 'chinese economy', '中国经济',
                     'stimulus', '经济刺激', 'pboc', 'lpr', '降准'],
        'sectors': ['整体市场', '半导体', '光通信', 'PCB'],
        'stocks': ['A股', '港股', '6869.HK', '300308.SZ', '300502.SZ',
                    '002384.SZ', '1888.HK', '600183.SH'],
        'description': '中国政策/经济刺激 → A股、港股、半导体、光模块、PCB产业链',
    },
    'energy_crisis': {
        'keywords': ['oil', 'energy crisis', '能源危机', 'supply disruption',
                     'fuel shortage', 'fuel', 'gasoline', 'gas price',
                     '石油', '原油', '燃料', '能源'],
        'sectors': ['能源/黄金', '整体市场', '新能源'],
        'stocks': ['2259.HK', '002353.SZ', '300438.SZ', '300750.SZ'],
        'description': '能源危机/油价冲击 → 原油服务、黄金、新能源/电池',
    },
    'supply_chain': {
        'keywords': ['supply chain', '供应链', 'shipping', '航运', 'port',
                     '港口', 'logistics', '物流', 'semiconductor shortage',
                     'chip shortage', '芯片短缺'],
        'sectors': ['半导体', '光通信/光模块', 'PCB/覆铜板'],
        'stocks': ['MU.O', 'SNDK.O', 'LITE.O', '300308.SZ', '300502.SZ',
                    '002384.SZ', '1888.HK', '600183.SH', '300476.SZ', '3200.HK'],
        'description': '供应链中断 → 半导体、光模块、PCB产业链',
    },
}

# --- RSS News Feeds for Geopolitical/Macro Monitoring ---
RSS_FEEDS = [
    ('BBC World', 'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('CNBC Top', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114'),
    ('Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml'),
    ('Reuters World', 'https://feeds.reuters.com/reuters/worldNews'),
    ('Reuters Business', 'https://feeds.reuters.com/reuters/businessNews'),
    ('SCMP China', 'https://www.scmp.com/rss/feed/podcasts-daily-china-business'),
    ('SCMP Post', 'https://www.scmp.com/rss/feed/3208556'),
    ('Caixin Global', 'https://www.caixin.com/rss/feed.xml'),
    # Chinese-language sources for domestic policy/macro news
    ('Xinhua Finance', 'http://www.news.cn/fortune/rss.xml'),
    ('Xinhua China', 'http://www.news.cn/politics/rss.xml'),
]

# --- Scheduled Macro Data Release Calendar Helper ---
def get_todays_macro_releases():
    """Return known macro data releases for today based on day-of-month rules."""
    today = datetime.now()
    releases = []
    weekday = today.weekday()  # 0=Mon
    day = today.day

    # ISM Manufacturing PMI: 1st business day of month
    if day <= 3 and weekday < 5:
        releases.append(('US', 'ISM Manufacturing PMI'))
    # ISM Services PMI: 3rd business day of month (roughly)
    if 3 <= day <= 5 and weekday < 5:
        releases.append(('US', 'ISM Services PMI'))
    # ADP Employment: first Wednesday
    if weekday == 2 and day <= 7:
        releases.append(('US', 'ADP Employment'))
    # JOLTS: usually Tuesday of first full week
    if weekday == 1:
        releases.append(('US', 'JOLTS Job Openings'))
    # EIA Crude Inventories: Wednesday
    if weekday == 2:
        releases.append(('US', 'EIA Crude Oil Inventories'))
    # Initial Jobless Claims: Thursday
    if weekday == 3:
        releases.append(('US', 'Initial Jobless Claims'))
    # Nonfarm Payrolls: first Friday
    if weekday == 4 and day <= 7:
        releases.append(('US', 'Nonfarm Payrolls'))
    # CPI: usually around 13th-15th (weekdays only)
    if 12 <= day <= 16 and weekday < 5:
        releases.append(('US', 'CPI'))
        releases.append(('US', 'Core CPI'))
    # PPI: day after CPI (weekdays only)
    if 13 <= day <= 17 and weekday < 5:
        releases.append(('US', 'PPI'))
    # Retail Sales: around 15th (weekdays only)
    if 14 <= day <= 17 and weekday < 5:
        releases.append(('US', 'Retail Sales'))
    # Fed FOMC decisions: ~6 weeks apart, approx dates
    fed_dates = [(26, 3), (6, 5), (17, 6), (29, 7), (16, 9), (28, 10), (9, 12)]
    for d, m in fed_dates:
        if day == d and today.month == m:
            releases.append(('US', 'FOMC Rate Decision'))

    # China: PMI on last business day of month
    if day >= 28 and day <= 31 and weekday < 5:
        releases.append(('CN', 'NBS Manufacturing PMI'))
        releases.append(('CN', 'Caixin Manufacturing PMI'))
    # China CPI/PPI: around 9th-12th (weekdays only — NBS doesn't release on weekends)
    if 9 <= day <= 12 and weekday < 5:
        releases.append(('CN', 'China CPI'))
        releases.append(('CN', 'China PPI'))
    # China Trade: around 13th-15th (weekdays only)
    if 13 <= day <= 15 and weekday < 5:
        releases.append(('CN', 'China Trade Balance'))
    # China GDP: ~15th of Jan/Apr/Jul/Oct (weekdays only)
    if day <= 18 and today.month in (1, 4, 7, 10) and weekday < 5:
        releases.append(('CN', 'China GDP'))

    return releases

# --- Commodity Mapping ---
# Keywords in stock name → commodity to monitor
# Uses Sina Finance commodity codes (hf_ = international, fixed contracts)
COMMODITY_KEYWORDS = {
    'gold': {'sina': 'hf_GC', 'label': 'COMEX黄金', 'name_en': 'Gold', 'threshold_pct': 3, 'yahoo_symbol': 'GC=F'},
    '紫金黄金': {'sina': 'hf_GC', 'label': 'COMEX黄金', 'name_en': 'Gold', 'threshold_pct': 3, 'yahoo_symbol': 'GC=F'},
}

def _extract_profit_change(title):
    match = re.search(r'(?:同比.*?)(?:增长|涨|升)[:：]*(\d+\.?\d*)%', title)
    if match:
        return float(match.group(1))
    match = re.search(r'(?:下降|降|减少|亏损扩大)[:：]*(\d+\.?\d*)%', title)
    if match:
        return -float(match.group(1))
    if "扭亏为盈" in title:
        return 999
    return None

def is_material(text):
    for p in NOISE_PATTERNS:
        if re.search(p, text):
            return False, "noise"
    # Check routine HKEX filings (English) — filter before material check
    for p in ROUTINE_HKEX_PATTERNS:
        if re.search(p, text, re.IGNORECASE):
            return False, "routine_hkex"
    pc = _extract_profit_change(text)
    if pc is not None:
        if pc == 999:
            return True, "扭亏为盈"
        elif pc >= PROFIT_GROWTH_THRESHOLD:
            return True, f"净利润同比+{pc:.0f}%"
        elif pc <= PROFIT_DECLINE_THRESHOLD:
            return True, f"净利润同比{pc:.0f}%"
        else:
            return False, "below threshold"
    for p in MATERIAL_PATTERNS:
        if re.search(p, text):
            return True, re.search(p, text).group()
    return False, "routine"

# --- Event Type Classification ---
def classify_event_type(event: dict) -> str:
    """Classify a pending event into a structured event_type for downstream routing.
    Returns one of: corporate_action, earnings, regulatory, macro, geopolitical,
                     commodity, portfolio_risk, competitor, cross_source, market_context
    """
    evt_type = event.get("type", "")
    reason = event.get("reason", "")
    title = event.get("title", "")
    combined = (title + " " + reason).lower()

    # Direct type mappings (Chinese types from layers 10-12)
    _direct_map = {
        "sec_filing": "regulatory",
        "earnings": "earnings",
        "competitor_earnings": "competitor",
        "competitor_news": "competitor",
        "commodity": "commodity",
        "macro": "macro",
        "portfolio_composition": "portfolio_risk",
        "cross_source_news": "cross_source",
        "财报预告": "earnings",
        "地缘政治风险": "geopolitical",
        "宏观突发事件": "macro",
        "市场背景": "market_context",
        "announcement": "regulatory",
        "hkex_announcement": "corporate_action",
        "central_bank_speech": "macro",
        "insider_trading": "corporate_action",
        "financial_press": "market_context",
    }
    if evt_type in _direct_map:
        return _direct_map[evt_type]

    # Heuristic classification for per_stock_news and generic types
    _corp_kw = ["收购", "并购", "重组", "合并", "借壳", "签订", "合同", "协议", "定增",
                "配股", "增发", "回购", "分红", "派息", "减持", "增持", "举牌", "权益变动",
                "退市", "停牌", "复牌", "破产", "重整", "清算", "要约", "投产", "新工厂"]
    _earnings_kw = ["业绩", "财报", "盈利", "亏损", "扭亏", "超预期", "不及预期", "EPS"]
    _regulatory_kw = ["被.*调查", "被.*立案", "被.*处罚", "罚款", "警示函", "监管",
                      "行政处罚", "反垄断", "环保", "安全", "诉讼", "仲裁", "违约"]
    _people_kw = ["董事长.*辞职", "CEO.*辞职", "总经理.*辞职", "高管", "实控人.*变更",
                  "控制人.*变更", "核心人员.*离职"]

    if any(kw in combined for kw in _people_kw):
        return "corporate_action"
    if any(kw in combined for kw in _earnings_kw):
        return "earnings"
    if any(re.search(kw, combined) for kw in _regulatory_kw):
        return "regulatory"
    if any(kw in combined for kw in _corp_kw):
        return "corporate_action"
    if any(kw in combined for kw in ["宏观", "利率", "fed", "fomc", "cpi", "gdp"]):
        return "macro"
    if any(kw in combined for kw in ["地缘", "战争", "冲突", "制裁"]):
        return "geopolitical"

    return "corporate_action"  # default fallback

# --- Discord Webhook Delivery ---
def send_to_discord(pending_events: list) -> int:
    """Send material events to Discord via webhook. Returns number of messages sent.
    Groups events by event_type, sends one embed per event (up to 10 per webhook call).
    Falls back to simple text if embeds fail.
    """
    webhook_url = get_portfolio_alerts_webhook()
    if not webhook_url:
        print("   ℹ️  #portfolio-alerts webhook not found — announcement delivery skipped", flush=True)
        return 0
    if not pending_events:
        return 0

    # Filter out geopolitical/macro events — these belong in the market brief channel,
    # not individual portfolio alerts. They are summarized by daily-market-summary.
    portfolio_events = [e for e in pending_events if e.get("event_type") not in ("geopolitical", "macro")]
    if len(portfolio_events) < len(pending_events):
        filtered = len(pending_events) - len(portfolio_events)
        print(f"   ℹ️  Filtering {filtered} geopolitical/macro event(s) → market-brief only", flush=True)
    if not portfolio_events:
        return 0
    pending_events = portfolio_events

    # Emoji map for event types
    _emoji = {
        "corporate_action": "🏢", "earnings": "📊", "regulatory": "⚖️",
        "macro": "🌐", "geopolitical": "🌍", "commodity": "📈",
        "portfolio_risk": "⚠️", "competitor": "🔄", "cross_source": "📰",
        "market_context": "📋",
    }
    # Color map (decimal for Discord embed)
    _color = {
        "corporate_action": 3447003,   # blue
        "earnings": 5763719,           # green
        "regulatory": 16753920,        # orange
        "macro": 15105570,             # gold
        "geopolitical": 15158332,      # red
        "commodity": 15844367,         # yellow
        "portfolio_risk": 10038562,    # purple
        "competitor": 2303786,         # teal
        "cross_source": 9807270,       # grey
        "market_context": 8421504,     # dark grey
    }

    sent = 0
    for event in pending_events[:10]:  # Discord rate limit safety
        evt_type = event.get("event_type", "corporate_action")
        emoji = _emoji.get(evt_type, "📌")
        color = _color.get(evt_type, 3447003)
        title = event.get("title", "Untitled event")[:256]
        stock = event.get("stock", "")
        code = event.get("code", "")
        name = event.get("name", "")
        reason = event.get("reason", "")
        url = event.get("url", "")
        source = event.get("source", "")
        time_str = event.get("time", "")
        content = event.get("content", "")[:400]

        # Build embed
        embed = {
            "title": f"{emoji} {title}",
            "color": color,
            "fields": [],
            "footer": {"text": f"Source: {source} | {time_str}"},
            "timestamp": datetime.utcnow().isoformat(),
        }
        if name:
            embed["fields"].append({"name": "Stock", "value": f"**{name}** ({code})", "inline": True})
        if reason:
            embed["fields"].append({"name": "Reason", "value": reason[:200], "inline": False})
        if content:
            embed["fields"].append({"name": "Detail", "value": content[:300], "inline": False})
        if url:
            embed["url"] = url[:512]

        payload = {
            "username": "Portfolio Alerts",
            "embeds": [embed],
        }

        try:
            r = requests.post(
                webhook_url,
                json=payload,
                timeout=15,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code in (200, 204):
                sent += 1
            else:
                print(f"   ⚠️ Discord HTTP {r.status_code}: {r.text[:100]}", flush=True)
            # Rate limit: 5 requests per 2 seconds
            time.sleep(0.5)
        except Exception as e:
            print(f"   ⚠️ Discord send error: {e}", flush=True)

    if sent > 0:
        print(f"   📤 Discord: sent {sent}/{len(pending_events)} alerts", flush=True)
    return sent


def send_news_report_to_discord(pending_events: list, target_channel_id: str = "1502241038579011655") -> int:
    """Send news report events to Discord, one message per category.
    Groups by event_type, formats as clean text (not embeds), sends each separately.
    Returns number of messages sent.
    """
    # Category map: event_type -> (emoji, title, sort_order)
    # Must match outputs from classify_event_type()
    CATEGORY_MAP = {
        "geopolitical": ("🌍", "地缘政治风险", 0),
        "macro": ("🏦", "央行/宏观", 1),
        "regulatory": ("⚖️", "监管/政策", 2),
        "earnings": ("📈", "盈利超预期", 3),
        "corporate_action": ("🏢", "公司行动", 4),
        "commodity": ("📈", "大宗商品", 5),
        "competitor": ("🔄", "竞争者动态", 6),
        "cross_source": ("📰", "交叉来源新闻", 7),
        "portfolio_risk": ("⚠️", "组合风险", 8),
        "market_context": ("📊", "市场背景", 9),
    }
    DEFAULT_CAT = ("📰", "其他新闻", 100)

    # Group events by category
    now_hkt = datetime.utcnow() + timedelta(hours=8)
    time_str = now_hkt.strftime("%H:%M HKT")

    groups = {}
    for event in pending_events:
        evt_type = event.get("event_type", "unknown")
        cat = CATEGORY_MAP.get(evt_type, DEFAULT_CAT)
        cat_key = cat[1]  # group by title
        if cat_key not in groups:
            groups[cat_key] = {"emoji": cat[0], "title": cat[1], "order": cat[2], "events": []}
        groups[cat_key]["events"].append(event)

    # Sort groups by order
    sorted_groups = sorted(groups.values(), key=lambda g: g["order"])

    # Get webhook URL
    webhook_url = get_portfolio_alerts_webhook()
    if not webhook_url:
        print("   ℹ️  Webhook not found — news report delivery skipped", flush=True)
        return 0

    sent = 0
    for group in sorted_groups:
        events = group["events"]
        if not events:
            continue

        # Build message text
        lines = [f"{group['emoji']} **{group['title']}** | {time_str}", ""]
        for ev in events:
            title = ev.get("title", "Untitled")
            time_ev = ev.get("time", "")
            reason = ev.get("reason", "")
            url = ev.get("url", "")
            source = ev.get("source", "")

            lines.append(f"• **{title}**")
            if time_ev:
                lines.append(f"  🕐 发布时间: {time_ev}")
            if reason:
                # Truncate reason to 2-3 sentences
                reason_text = reason[:300]
                lines.append(f"  影响: {reason_text}")
            if url:
                src_label = source if source else "来源"
                lines.append(f"  📎 {src_label}: {url}")
            lines.append("")

        message_text = "\n".join(lines).strip()

        # Truncate if too long (Discord 2000 char limit)
        if len(message_text) > 1900:
            message_text = message_text[:1897] + "..."

        # Send via webhook
        payload = {
            "content": message_text,
            "username": "组合新闻快报",
        }

        try:
            r = requests.post(
                webhook_url,
                json=payload,
                timeout=15,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code in (200, 204):
                sent += 1
                print(f"   📤 Discord: sent '{group['title']}' ({len(events)} events)", flush=True)
            else:
                print(f"   ⚠️ Discord HTTP {r.status_code}: {r.text[:100]}", flush=True)
            time.sleep(0.5)  # Rate limit
        except Exception as e:
            print(f"   ⚠️ Discord send error: {e}", flush=True)

    return sent


# --- State Management ---
def load_portfolio_state():
    if not PORTFOLIO_STATE_PATH.exists():
        return {}
    try:
        return json.loads(PORTFOLIO_STATE_PATH.read_text())
    except:
        return {}

def get_us_stocks():
    """Get US stocks from portfolio with their codes."""
    state = load_portfolio_state()
    us = {}
    for code, info in state.items():
        if code.endswith(".O"):
            ticker = code.replace(".O", "").upper()
            us[ticker] = {"code": code, "name": info.get("name", "")}
    return us

def get_a_shares():
    state = load_portfolio_state()
    return {k: v for k, v in state.items() if k.endswith((".SH", ".SZ"))}

def get_hk_stocks():
    state = load_portfolio_state()
    return {k: v for k, v in state.items() if k.endswith(".HK")}

def load_news_history():
    today = datetime.now().strftime("%Y-%m-%d")
    if not NEWS_HISTORY_PATH.exists():
        return {"date": today, "seen": []}
    try:
        data = json.loads(NEWS_HISTORY_PATH.read_text())
        if data.get("date") != today:
            return {"date": today, "seen": []}
        return data
    except:
        return {"date": today, "seen": []}

def save_news_history(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    NEWS_HISTORY_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# --- Auto-Discovery: Competitors ---
def discover_competitors(us_stocks, force_refresh=False):
    """Auto-discover competitors for US portfolio stocks via Finnhub peers API.
    Returns {ticker: [peer_symbols, ...]} cache.
    Refreshes weekly or on force.
    """
    cache = {}
    if COMPETITOR_CACHE_PATH.exists() and not force_refresh:
        try:
            cache_data = json.loads(COMPETITOR_CACHE_PATH.read_text())
            cached_at = cache_data.get("updated", "")
            # Refresh weekly
            if cached_at:
                cached_date = datetime.strptime(cached_at, "%Y-%m-%d")
                if (datetime.now() - cached_date).days < 7:
                    cache = cache_data.get("peers", {})
                    print(f"   📋 Using cached competitors (updated {cached_at})", flush=True)
        except:
            pass

    if not cache:
        print("   🔍 Discovering competitors via Finnhub peers API...", flush=True)
        for ticker in us_stocks:
            try:
                r = requests.get(
                    f"https://finnhub.io/api/v1/stock/peers?symbol={ticker}&token={FINNHUB_API_KEY}",
                    timeout=10
                )
                peers = r.json()
                if isinstance(peers, list):
                    # Filter out the stock itself and keep top 5 peers
                    peer_list = [p for p in peers if p != ticker][:5]
                    cache[ticker] = peer_list
                    print(f"     {ticker} → {', '.join(peer_list)}", flush=True)
                time.sleep(0.5)
            except Exception as e:
                print(f"     ⚠️ {ticker} peers error: {e}", flush=True)
                cache[ticker] = []

        # Save cache
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        COMPETITOR_CACHE_PATH.write_text(json.dumps({
            "updated": datetime.now().strftime("%Y-%m-%d"),
            "peers": cache
        }, indent=2))

    return cache

# --- Auto-Discovery: Commodities ---
def discover_commodities():
    """Map portfolio holdings to relevant commodities by keyword matching."""
    state = load_portfolio_state()
    relevant = {}
    for code, info in state.items():
        name = info.get("name", "")
        for keyword, mapping in COMMODITY_KEYWORDS.items():
            if keyword.lower() in name.lower() or keyword in name:
                sina_code = mapping['sina']
                if sina_code not in relevant:
                    relevant[sina_code] = {
                        'label': mapping['label'],
                        'name_en': mapping['name_en'],
                        'threshold_pct': mapping['threshold_pct'],
                        'affected_stocks': [],
                    }
                relevant[sina_code]['affected_stocks'].append(f"{code} ({name})")
    return relevant

# --- AKShare Helpers ---
def _find_akshare_python():
    import subprocess
    for p in ["/usr/bin/python3", "/usr/local/bin/python3"]:
        try:
            r = subprocess.run([p, "-c", "import akshare; print(1)"],
                             capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except:
            continue
    return None

def _run_akshare_news(python_path, stock_code):
    import subprocess
    script = f"""
import akshare as ak
import json
try:
    df = ak.stock_news_em(symbol='{stock_code}')
    records = df.to_dict(orient='records')
    print(json.dumps(records, ensure_ascii=False))
except:
    print(json.dumps([]))
"""
    try:
        r = subprocess.run([python_path, "-c", script],
                         capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
        return []
    except:
        return []

# --- Detection Layers ---
def check_per_stock_news(seen_ids, pending):
    """Layer 1: Per-stock A-share + HK news via AKShare."""
    state = load_portfolio_state()
    ak_python = _find_akshare_python()
    if not ak_python:
        return

    # A-share codes
    a_codes = load_portfolio_codes_a()
    # HK codes (pad to 5 digits for AKShare)
    hk_codes = {}
    for code, info in state.items():
        if code.endswith(".HK"):
            raw = code.replace(".HK", "").zfill(5)
            hk_codes[raw] = {"code": code, "name": info.get("name", "")}

    all_codes = list(a_codes) + list(hk_codes.keys())
    if not all_codes:
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    def fetch_news(code):
        return code, _run_akshare_news(ak_python, code)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_news, code): code for code in all_codes}
        results = {}
        for future in as_completed(futures):
            code = futures[future]
            try:
                c, result = future.result(timeout=30)
                results[c] = result
            except:
                results[c] = []

    for code in a_codes:
        _process_akshare_results(code, results.get(code, []), seen_ids, pending)

    for code in hk_codes:
        _process_akshare_results(code, results.get(code, []), seen_ids, pending,
                                 stock_code=code.replace(".HK","").zfill(5) + ".HK",
                                 name=hk_codes[code].get("name", ""))

def _is_recent(pub_time, max_hours=24):
    """Check if a news item's publish time is within max_hours.
    AKShare returns news sorted by relevance, not strictly by time.
    Filter out stale articles that would otherwise be treated as new.
    """
    if not pub_time:
        return False  # No timestamp → reject to avoid stale news
    now = datetime.now()
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"]:
        try:
            dt = datetime.strptime(pub_time.strip(), fmt)
            # If no year in the string, assume current year
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            if (now - dt).total_seconds() <= max_hours * 3600:
                return True
            return False
        except ValueError:
            continue
    return False  # Can't parse → reject

def _process_akshare_results(code, result, seen_ids, pending, stock_code=None, name=""):
    """Process AKShare news results with materiality filtering."""
    if not stock_code:
        stock_code = code + ".SH" if code.startswith("6") or code.startswith("1") else code + ".SZ"
    if not result:
        return
    for item in result[:5]:  # Check more items, filter by freshness
        title = item.get("新闻标题", "")
        pub_time = item.get("发布时间", "")
        source = item.get("文章来源", "")
        content = item.get("新闻内容", "")[:300]
        url = item.get("新闻链接", "")
        if not title:
            continue
        # Filter out stale news: must be published within last 24 hours
        if not _is_recent(pub_time, max_hours=24):
            continue
        mat, reason = is_material(title + " " + source)
        if not mat:
            continue
        item_id = f"aks:{code}:{title[:50]}"
        if item_id in seen_ids:
            continue
        # Dedup by event
        pc = _extract_profit_change(title)
        if pc is not None:
            direction = "up" if pc > 0 else "down"
            rounded = round(pc / 10) * 10
            event_key = f"event:{code}:{direction}:{rounded}"
            if event_key in seen_ids:
                continue
            seen_ids.add(event_key)
        seen_ids.add(item_id)
        pending.append({
            "type": "per_stock_news",
            "stock": stock_code,
            "code": stock_code,
            "name": name,
            "title": title,
            "url": url,
            "content": content,
            "source": source,
            "time": pub_time,
            "reason": reason,
        })
    time.sleep(0.1)

def check_sec_filings(seen_ids, pending):
    """Layer 2: SEC 8-K filings for US portfolio stocks.
    CIKs auto-discovered via SEC company_tickers.json, cached daily.
    """
    state = load_portfolio_state()
    # Get CIKs for ALL US stocks via auto-discovery
    cik_cache = get_sec_cik_cache()
    if not cik_cache:
        print("   ⚠️ SEC CIK cache empty — skipping SEC monitoring", flush=True)
        return

    for code, info in state.items():
        if not code.endswith(".O"):
            continue
        ticker = code.replace(".O", "")
        cik = cik_cache.get(ticker)
        if not cik:
            print(f"     ℹ️  No CIK found for {ticker}", flush=True)
            continue
        name = info.get("name", "")
        rss_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=5&output=atom"
        )
        try:
            r = requests.get(rss_url, headers={"User-Agent": "Portfolio Monitor (test@example.com)"}, timeout=15)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                title_elem = entry.find("atom:title", ns)
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                ft_match = re.match(r'^([0-9]+-[0-9]+|[0-9]+)', title)
                if not ft_match:
                    continue
                filing_type = ft_match.group(1)
                if filing_type != "8-K":
                    continue
                link_elem = entry.find("atom:link", ns)
                link = link_elem.get("href", "") if link_elem is not None else ""
                item_id = f"sec:{ticker}:{filing_type}:{link[-20:]}"
                if item_id in seen_ids:
                    continue
                updated_elem = entry.find("atom:updated", ns)
                if updated_elem is not None:
                    updated_date = datetime.strptime(updated_elem.text[:10], "%Y-%m-%d")
                    if (datetime.now() - updated_date).days > 7:
                        continue
                seen_ids.add(item_id)
                pending.append({
                    "type": "sec_filing",
                    "stock": code,
                    "code": code,
                    "name": name,
                    "title": title,
                    "filing_type": filing_type,
                    "url": link,
                    "content": "",
                    "source": "SEC EDGAR",
                    "time": updated_elem.text[:16] if updated_elem is not None else "",
                    "reason": "SEC 8-K 重大事件报告",
                })
        except Exception as e:
            print(f"   ⚠️ SEC error {ticker}: {e}", flush=True)
        time.sleep(0.3)

def check_earnings(seen_ids, pending):
    """Layer 3: Earnings surprises for US portfolio stocks (>5% beat/miss)."""
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    state = load_portfolio_state()
    try:
        url = f"https://finnhub.io/api/v1/calendar/earnings?from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return
        data = r.json()
        for e in data.get("earningsCalendar", []):
            sym = e.get("symbol", "")
            code = sym + ".O"
            if code not in state:
                continue
            info = state[code]
            eps_est = e.get("epsEstimate")
            eps_act = e.get("epsActual")
            if eps_est is None or eps_act is None or eps_est == 0:
                continue
            surprise_pct = (eps_act - eps_est) / abs(eps_est)
            if abs(surprise_pct) < 0.05:
                continue
            item_id = f"earnings:{sym}:{today}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            hour_label = {"bmo": "盘前", "amc": "盘后", "dmh": "盘中"}.get(e.get("hour", ""), "")
            direction = "beat" if surprise_pct > 0 else "miss"
            pending.append({
                "type": "earnings",
                "stock": code,
                "code": code,
                "name": info.get("name", ""),
                "title": f"EPS {direction}: {eps_act} vs {eps_est} ({surprise_pct*100:+.1f}%)",
                "url": f"https://finance.yahoo.com/quote/{sym}/",
                "content": f"EPS Actual: {eps_act}, Estimate: {eps_est}, Surprise: {surprise_pct*100:+.1f}%, Time: {hour_label}, Revenue Estimate: {e.get('revenueEstimate','N/A')}",
                "source": "Finnhub",
                "time": f"{today} {hour_label}",
                "reason": f"财报{'超预期' if surprise_pct > 0 else '不及预期'} {abs(surprise_pct)*100:.1f}%",
            })
    except Exception as e:
        print(f"   ⚠️ Earnings error: {e}", flush=True)

def check_ashare_announcements(seen_ids, pending):
    """Layer 4: A-share official announcements (material only)."""
    codes = load_portfolio_codes_a()
    if not codes:
        return
    ak_python = _find_akshare_python()
    if not ak_python:
        return
    today = datetime.now().strftime("%Y%m%d")
    item_id_today = f"aks_notice:{today}"
    if item_id_today in seen_ids:
        return
    import subprocess
    script = f"""
import akshare as ak
import json
try:
    df = ak.stock_notice_report(symbol='全部', date='{today}')
    portfolio_codes = {json.dumps(list(codes))}
    df_filtered = df[df['代码'].isin(portfolio_codes)]
    skip_types = ['年度报告', '半年度报告', '季度报告', '审计报告',
                  '内部控制', '募集资金', '独立董事', '社会责任',
                  '可持续发展', 'ESG', '调研活动', '路演']
    for skip in skip_types:
        df_filtered = df_filtered[~df_filtered['公告类型'].str.contains(skip, na=False)]
    records = df_filtered.head(10).to_dict(orient='records')
    print(json.dumps(records, ensure_ascii=False, default=str))
except Exception as e:
    print(json.dumps([]))
"""
    try:
        r = subprocess.run([ak_python, "-c", script],
                         capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout.strip())
            alerts_per_stock = {}
            for item in data:
                code = item.get("代码", "")
                if not code or alerts_per_stock.get(code, 0) >= 2:
                    continue
                title = item.get("公告标题", "")
                ann_type = item.get("公告类型", "")
                mat, reason = is_material(title + " " + ann_type)
                if not mat:
                    continue
                url = item.get("网址", "")
                item_id = f"aks_ann:{code}:{title[:50]}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                alerts_per_stock[code] = alerts_per_stock.get(code, 0) + 1
                stock_code = code + ".SH" if code.startswith("6") else code + ".SZ"
                pending.append({
                    "type": "announcement",
                    "stock": stock_code,
                    "code": stock_code,
                    "name": "",
                    "title": title,
                    "url": f"https://data.eastmoney.com/notices/detail/{code}/{url}" if url else "",
                    "content": f"类型: {ann_type}, 日期: {item.get('公告日期', '')}",
                    "source": "东方财富",
                    "time": item.get("公告日期", ""),
                    "reason": reason,
                })
            seen_ids.add(item_id_today)
    except Exception as e:
        print(f"   ⚠️ A-Share announcement error: {e}", flush=True)

def check_competitor_events(seen_ids, pending):
    """Layer 5: Competitor earnings and news (auto-discovered)."""
    us_stocks = get_us_stocks()
    state = load_portfolio_state()
    competitor_cache = discover_competitors(us_stocks)

    if not competitor_cache:
        print("   ⏭️ No competitors discovered", flush=True)
        return

    print("   🔍 Checking competitor earnings & news...", flush=True)
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    ago = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 5a. Competitor earnings + guidance analysis
    try:
        url = f"https://finnhub.io/api/v1/calendar/earnings?from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            all_peers = set()
            for peers in competitor_cache.values():
                all_peers.update(peers)
            for e in data.get("earningsCalendar", []):
                sym = e.get("symbol", "")
                if sym not in all_peers:
                    continue
                eps_est = e.get("epsEstimate")
                eps_act = e.get("epsActual")
                surprise_pct = 0
                if eps_est and eps_act and eps_est != 0:
                    surprise_pct = (eps_act - eps_est) / abs(eps_est)
                # Always alert for competitor earnings
                affected = []
                for ticker, peers in competitor_cache.items():
                    if sym in peers and ticker in state:
                        affected.append(f"{ticker}.O ({state[ticker+'.O']['name']})")
                if not affected:
                    continue
                item_id = f"comp_earnings:{sym}:{today}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                hour_label = {"bmo": "盘前", "amc": "盘后", "dmh": "盘中"}.get(e.get("hour", ""), "")
                eps_str = f"EPS: {eps_act} vs {eps_est}" if eps_est else "EPS: N/A"
                surprise_str = f" (surprise {surprise_pct*100:+.1f}%)" if eps_est and abs(surprise_pct) >= 0.05 else ""

                # Try to get guidance/forward-looking content from Finnhub press releases
                guidance_text = ""
                try:
                    pr_url = f"https://finnhub.io/api/v1/press-releases?symbol={sym}&from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
                    pr_r = requests.get(pr_url, timeout=10)
                    if pr_r.status_code == 200:
                        pr_data = pr_r.json()
                        if isinstance(pr_data, list) and pr_data:
                            pr_text = pr_data[0].get("content", "")[:500]
                            if pr_text:
                                # Look for guidance keywords
                                guidance_kw = ['guidance', 'outlook', 'expect', 'forecast', 'raise', 'lower',
                                               'full year', 'FY20', 'Q2', 'Q3', 'Q4', 'target', 'project']
                                for line in pr_text.split('.')[:10]:
                                    if any(kw in line.lower() for kw in guidance_kw):
                                        guidance_text += line.strip()[:200] + ". "
                except:
                    pass

                content_parts = [f"Time: {hour_label}, Revenue Est: {e.get('revenueEstimate','N/A')}",
                                f"EPS Est: {eps_est}, Actual: {eps_act}"]
                if guidance_text:
                    content_parts.append(f"Guidance: {guidance_text[:300]}")

                pending.append({
                    "type": "competitor_earnings",
                    "stock": sym,
                    "code": sym,
                    "name": sym,
                    "title": f"竞争对手财报: {sym} {eps_str}{surprise_str}",
                    "url": f"https://finance.yahoo.com/quote/{sym}/",
                    "content": "\\n".join(content_parts),
                    "source": "Finnhub",
                    "time": f"{today} {hour_label}",
                    "reason": f"竞争对手{sym}发布财报{'含前瞻指引' if guidance_text else ''}，影响: {', '.join(affected)}",
                })
    except Exception as e:
        print(f"   ⚠️ Competitor earnings error: {e}", flush=True)

    # 5b. Competitor news (material only, freshness-filtered)
    cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
    for ticker, peers in competitor_cache.items():
        for peer in peers:
            try:
                url = f"https://finnhub.io/api/v1/company-news?symbol={peer}&from={ago}&to={today}&token={FINNHUB_API_KEY}"
                r = requests.get(url, timeout=10)
                if r.status_code != 200:
                    continue
                news_items = r.json()
                if not isinstance(news_items, list):
                    continue
                for item in news_items[:5]:  # Scan more, filter by freshness
                    title = item.get("headline", "")
                    if not title:
                        continue
                    # Freshness filter: skip news older than 24 hours
                    item_dt = item.get("datetime", 0)
                    if item_dt and item_dt < cutoff_ts:
                        continue
                    mat, reason = is_material(title)
                    if not mat:
                        continue
                    news_id = item.get("id", "")
                    item_id = f"comp_news:{peer}:{news_id}"
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    affected = []
                    for t, ps in competitor_cache.items():
                        if peer in ps and t in state:
                            affected.append(f"{t}.O ({state[t+'.O']['name']})")
                    if not affected:
                        continue
                    source = item.get("source", "")
                    item_url = item.get("url", "")
                    pending.append({
                        "type": "competitor_news",
                        "stock": peer,
                        "code": peer,
                        "name": peer,
                        "title": f"竞争对手新闻: {peer} — {title[:100]}",
                        "url": item_url,
                        "content": title,
                        "source": source,
                        "time": datetime.fromtimestamp(item.get("datetime", 0)).strftime("%Y-%m-%d %H:%M"),
                        "reason": f"竞争对手{peer}重要新闻，影响: {', '.join(affected)}",
                    })
                time.sleep(0.3)
            except Exception as e:
                print(f"   ⚠️ Competitor news error {peer}: {e}", flush=True)

def check_commodity_prices(seen_ids, pending):
    """Layer 6: Commodity price spikes mapped to portfolio holdings.
    Checks both intraday change (vs prev_close) and 24h range (high-low).
    Falls back to Yahoo Finance if Sina data is stale/zero.
    """
    commodities = discover_commodities()
    if not commodities:
        return
    print(f"   📊 Checking commodities: {', '.join(c['label'] for c in commodities.values())}...", flush=True)
    symbols = ','.join(commodities.keys())
    now = datetime.now()
    today_label = now.strftime('%Y-%m-%d')
    try:
        r = requests.get(f'http://hq.sinajs.cn/list={symbols}',
                        headers={'Referer': 'https://finance.sina.com.cn'}, timeout=10)
        r.encoding = 'gbk'
        for line in r.text.strip().split('\n'):
            if not line.strip() or '""' in line:
                continue
            parts = line.split('="')
            if len(parts) < 2:
                continue
            raw_symbol = parts[0].split('hq_str_')[-1]
            if raw_symbol not in commodities:
                continue
            comm = commodities[raw_symbol]
            fields = parts[1].strip('";').split(',')
            if len(fields) < 4:
                continue
            # Sina commodity format varies:
            # Futures (hf_ prefix): 15 fields typically: [0]=price, [1]=empty, [2]=prev_close, [3]=current, 
            #   [4]=high, [5]=low, [6]=time, [7-11]=other, [12]=date, [13]=name, [14]=flag
            #   Some responses may have 13 fields with last field = "date,name,flag" combined
            # Spot (stock/index):   [0]=name,  [1]=prev_close, [2]=current, [3]=open, [4]=high, [5]=low
            is_futures = raw_symbol.startswith('hf_')
            
            # Parse futures vs spot with correct field indices
            if is_futures:
                # Try 15-field format first (fields[13] = name)
                if len(fields) >= 14 and fields[13]:
                    # Standard 15-field format
                    pass  # fields[13] already has the name
                elif len(fields) >= 13:
                    # Compact 13-field format: last field = "date,name,flag"
                    # Can be ASCII comma (,) or Chinese comma (,)
                    last_field = fields[-1]
                    for delim in [',', ',']:
                        if delim in last_field:
                            sub_fields = last_field.split(delim)
                            if len(sub_fields) >= 3:
                                # Reconstruct fields list with proper indices
                                fields = fields[:-1] + sub_fields
                                break
                # Parse price fields (same for both formats)
                try:
                    prev_close = float(fields[2]) if fields[2] else 0
                    current_price = float(fields[3]) if fields[3] else 0
                    high_price = float(fields[4]) if len(fields) > 4 and fields[4] else 0
                    low_price = float(fields[5]) if len(fields) > 5 and fields[5] else 0
                except (ValueError, IndexError):
                    continue
            else:
                # Spot parsing
                try:
                    current_price = float(fields[2]) if fields[2] else 0
                    prev_close = float(fields[1]) if fields[1] else 0
                    high_price = float(fields[4]) if len(fields) > 4 and fields[4] else 0
                    low_price = float(fields[5]) if len(fields) > 5 and fields[5] else 0
                except (ValueError, IndexError):
                    continue

            threshold = comm.get('threshold_pct', 3)
            events_fired = []

            if current_price > 0 and prev_close > 0:
                # Check 1: Intraday change vs prev_close
                pct_change = (current_price - prev_close) / prev_close * 100
                if abs(pct_change) >= threshold:
                    direction = "上涨" if pct_change > 0 else "下跌"
                    item_id = f"comm:{raw_symbol}:{today_label}:intraday"
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        events_fired.append({
                            "type": "commodity",
                            "stock": comm['label'],
                            "code": comm['label'],
                            "name": comm['name_en'],
                            "title": f"{comm['label']} {direction} {abs(pct_change):.1f}% (现价: {current_price:.2f})",
                            "url": "",
                            "content": f"Previous: {prev_close:.2f}, Current: {current_price:.2f}, Intraday Change: {pct_change:+.1f}%",
                            "source": "Sina Finance",
                            "time": now.strftime("%Y-%m-%d %H:%M"),
                            "reason": f"{comm['label']}日内波动超阈值({threshold}%)，影响: {', '.join(comm['affected_stocks'])}",
                        })
                        print(f"     ⚠️ {comm['label']}: intraday {pct_change:+.1f}% (threshold: ±{threshold}%)", flush=True)

                # Check 2: 24h range (high - low as % of prev_close)
                if high_price > 0 and low_price > 0 and prev_close > 0:
                    range_pct = (high_price - low_price) / prev_close * 100
                    if range_pct >= threshold * 1.5:  # 1.5x threshold for range (less strict)
                        item_id = f"comm:{raw_symbol}:{today_label}:range"
                        if item_id not in seen_ids:
                            seen_ids.add(item_id)
                            events_fired.append({
                                "type": "commodity",
                                "stock": comm['label'],
                                "code": comm['label'],
                                "name": comm['name_en'],
                                "title": f"{comm['label']} 日内振幅 {range_pct:.1f}% (高:{high_price:.2f} 低:{low_price:.2f})",
                                "url": "",
                                "content": f"24h Range: High={high_price:.2f}, Low={low_price:.2f}, Range={range_pct:.1f}%, Current={current_price:.2f}",
                                "source": "Sina Finance",
                                "time": now.strftime("%Y-%m-%d %H:%M"),
                                "reason": f"{comm['label']}日内振荡剧烈({range_pct:.1f}%)，影响: {', '.join(comm['affected_stocks'])}",
                            })
                            print(f"     ⚠️ {comm['label']}: range {range_pct:.1f}% (threshold: {threshold*1.5:.1f}%)", flush=True)

            # Check 3: Yahoo Finance fallback for 24h range + overnight moves
            # Always try Yahoo for commodities even when Sina has data
            # (Yahoo captures overnight/official settlement moves that Sina intraday misses)
            if 'yahoo_symbol' in comm:
                try:
                    yahoo_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{comm['yahoo_symbol']}?range=2d&interval=1d"
                    yh = requests.get(yahoo_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                    if yh.status_code == 200:
                        ydata = yh.json()
                        result = ydata.get('chart', {}).get('result', [{}])[0]
                        meta = result.get('meta', {})
                        y_prev_close = meta.get('chartPreviousClose', 0) or meta.get('previousClose', 0)
                        y_current = meta.get('regularMarketPrice', 0)

                        # Also get day high/low from indicators
                        indicators = result.get('indicators', {}).get('quote', [{}])[0]
                        y_highs = [h for h in indicators.get('high', []) if h is not None]
                        y_lows = [l for l in indicators.get('low', []) if l is not None]

                        if y_current > 0 and y_prev_close > 0:
                            y_pct = (y_current - y_prev_close) / y_prev_close * 100
                            yahoo_item_id = f"comm_yf:{raw_symbol}:{today_label}"

                            # Check 3a: Yahoo intraday change (captures overnight moves)
                            if abs(y_pct) >= threshold and yahoo_item_id not in seen_ids:
                                seen_ids.add(yahoo_item_id)
                                direction = "上涨" if y_pct > 0 else "下跌"
                                events_fired.append({
                                    "type": "commodity",
                                    "stock": comm['label'],
                                    "code": comm['label'],
                                    "name": comm['name_en'],
                                    "title": f"{comm['label']} {direction} {abs(y_pct):.1f}% (Yahoo: {y_current:.2f})",
                                    "url": "",
                                    "content": f"Yahoo Finance: Previous={y_prev_close:.2f}, Current={y_current:.2f}, Change={y_pct:+.1f}%",
                                    "source": "Yahoo Finance",
                                    "time": now.strftime("%Y-%m-%d %H:%M"),
                                    "reason": f"{comm['label']}波动超阈值({threshold}%) [Yahoo]，影响: {', '.join(comm['affected_stocks'])}",
                                })
                                print(f"     ⚠️ {comm['label']}: Yahoo {y_pct:+.1f}% (threshold: {threshold}%)", flush=True)

                            # Check 3b: Yahoo 24h range (high-low of recent sessions)
                            if y_highs and y_lows and y_prev_close > 0:
                                y_max_h = max(y_highs)
                                y_min_l = min(y_lows)
                                y_range_pct = (y_max_h - y_min_l) / y_prev_close * 100
                                if y_range_pct >= threshold * 1.2 and yahoo_item_id not in seen_ids:
                                    seen_ids.add(yahoo_item_id)
                                    events_fired.append({
                                        "type": "commodity",
                                        "stock": comm['label'],
                                        "code": comm['label'],
                                        "name": comm['name_en'],
                                        "title": f"{comm['label']} 24h振幅 {y_range_pct:.1f}% (高:{y_max_h:.2f} 低:{y_min_l:.2f})",
                                        "url": "",
                                        "content": f"Yahoo 2d Range: High={y_max_h:.2f}, Low={y_min_l:.2f}, Range={y_range_pct:.1f}%, Current={y_current:.2f}",
                                        "source": "Yahoo Finance",
                                        "time": now.strftime("%Y-%m-%d %H:%M"),
                                        "reason": f"{comm['label']}24小时振幅剧烈({y_range_pct:.1f}%)，影响: {', '.join(comm['affected_stocks'])}",
                                    })
                                    print(f"     ⚠️ {comm['label']}: 24h range {y_range_pct:.1f}% (threshold: {threshold*1.2:.1f}%)", flush=True)
                except Exception as yf_err:
                    print(f"     ℹ️ Yahoo fallback for {comm.get('yahoo_symbol','?')}: {yf_err}", flush=True)

            # Add all events
            for ev in events_fired:
                pending.append(ev)

    except Exception as e:
        print(f"   ⚠️ Commodity check error: {e}", flush=True)

def check_macro_events(seen_ids, pending):
    """Layer 7: High-impact macro events.
    Sources:
    - Finnhub economic calendar (scheduled data releases)
    - Finnhub market-news macro scan (breaking macro news)
    - Local calendar helper for known release dates
    """
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    today_label = today

    # --- 7a. Scheduled Macro Releases (local calendar) ---
    scheduled_releases = get_todays_macro_releases()
    for country, release_name in scheduled_releases:
        item_id = f"macro_scheduled:{country}:{release_name}:{today_label}"
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        impact_map = {
            'US': "影响: US科技股(MU/SNDK/LITE)、黄金(2259.HK)、A股整体情绪",
            'CN': "影响: A股整体流动性、半导体、港股",
        }
        pending.append({
            "type": "macro",
            "stock": f"{country} Macro",
            "code": f"{country}_MACRO",
            "name": f"{country} Macro",
            "title": f"今日宏观数据: {country} {release_name}",
            "url": "",
            "content": f"Scheduled release: {release_name} ({country}). Check for actual vs estimate after release.",
            "source": "Macro Calendar",
            "time": today_label,
            "reason": f"今日计划发布{country}{release_name}，可能影响市场 {impact_map.get(country, '')}",
        })
        print(f"     📅 Scheduled macro: {country} {release_name}", flush=True)

    # --- 7b. Finnhub Economic Calendar (original logic, enhanced) ---
    try:
        url = f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={tomorrow}&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for e in data.get("economicCalendar", []):
                event = e.get("event", "").lower()
                country = e.get("country", "")
                # Only high-impact events
                high_impact = [
                    'interest rate', 'rate decision', 'fomc', 'cpi', 'ppc',
                    'nonfarm', 'unemployment', 'gdp', 'retail sales',
                    'tariff', 'export control', 'sanction', 'trade war',
                    'trade balance', 'consumer confidence', 'pmi',
                    'mlf', 'rrr', '贷款市场报价利率', 'lpr',
                ]
                is_high = any(h in event for h in high_impact)
                actual = e.get("actual")
                estimate = e.get("estimate")
                surprise = 0
                if actual and estimate:
                    try:
                        surprise = abs(float(actual) - float(estimate))
                    except:
                        surprise = 0
                # Alert if: high-impact event OR actual vs estimate miss > threshold
                is_material_macro = is_high and (
                    (country in ['US', 'CN'] and surprise > 0.3) or  # CPI/Rate miss
                    (country == 'US' and ('rate' in event or 'fomc' in event)) or  # Fed rate
                    (country == 'CN' and ('lpr' in event or 'mlf' in event)) or  # PBOC rate
                    ('tariff' in event or 'export control' in event or 'sanction' in event)  # Trade policy
                )
                if not is_material_macro:
                    continue
                item_id = f"macro:{country}:{event[:30]}:{today_label}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                if country == 'US':
                    impact = "影响: US科技股(MU/SNDK/LITE)、黄金(2259.HK)、A股整体情绪"
                elif country == 'CN':
                    impact = "影响: A股整体流动性、半导体(MU间接)、新能源(300750)"
                else:
                    impact = ""
                pending.append({
                    "type": "macro",
                    "stock": f"{country} Macro",
                    "code": f"{country}_MACRO",
                    "name": f"{country} Macro",
                    "title": f"宏观事件: {country} {e.get('event', '')}",
                    "url": "",
                    "content": f"Actual: {actual}, Estimate: {estimate}, Previous: {e.get('previous', '')}",
                    "source": "Finnhub Economic Calendar",
                    "time": e.get("date", ""),
                    "reason": f"重要宏观数据发布 {impact}",
                })
    except Exception as e:
        print(f"   ⚠️ Finnhub calendar error: {e}", flush=True)

    # --- 7c. Finnhub Market News Macro Scan (breaking macro news, no ticker filter) ---
    # Tiered: CRITICAL terms (1 match → fire), STANDARD terms (2+ matches → fire)
    MACRO_CRITICAL = [
        'fed', 'federal reserve', 'fomc', 'powell', 'nonfarm payrolls',
        'rate hike', 'rate cut', 'interest rate decision',
        'cpi', 'consumer price index', 'inflation',
        '美联储', '加息', '降息', '利率决议', 'cpi数据', '非农',
    ]
    MACRO_STANDARD = [
        'gdp', 'unemployment', 'pmi', '制造业', '服务业',
        '就业', 'retail sales', 'consumer confidence',
        'durable goods', 'initial claims', 'jobless claims', 'weekly jobless',
        'treasury yield', 'bond yield', '国债收益率', 'yield curve',
        'quantitative easing', 'tightening', 'stimulus', '经济刺激',
        'budget', 'deficit', 'debt ceiling', '债务上限', '政府关门',
    ]
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            items = r.json()
            if isinstance(items, list):
                found_macro = 0
                for item in items[:15]:
                    headline = item.get("headline", "")
                    summary = item.get("summary", "")
                    full_text = (headline + " " + summary).lower()
                    # Check for macro keywords with tiered threshold
                    critical_matches = [kw for kw in MACRO_CRITICAL if kw.lower() in full_text]
                    standard_matches = [kw for kw in MACRO_STANDARD if kw.lower() in full_text]
                    # CRITICAL: 1 match fires. STANDARD: 2+ matches fire
                    if len(critical_matches) < 1 and len(standard_matches) < 2:
                        continue
                    matched_kws = critical_matches + standard_matches[:3]
                    # Skip if it's stock-specific (picked up by other layers)
                    if any(t in full_text for t in ['upgrade', 'downgrade', 'target', 'rating']):
                        continue
                    item_id = f"macro_news:{str(item.get('id', ''))[:20]}:{today_label}"
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    source = item.get("source", "Finnhub")
                    pub_time = item.get("datetime", 0)
                    time_str = datetime.fromtimestamp(pub_time).strftime("%Y-%m-%d %H:%M") if pub_time else today_label
                    pending.append({
                        "type": "宏观突发事件",
                        "stock": "MACRO",
                        "code": "MACRO",
                        "name": "宏观市场",
                        "title": f"[宏观] {headline[:150]}",
                        "url": item.get("url", ""),
                        "content": summary[:800],
                        "source": source,
                        "time": time_str,
                        "reason": f"宏观突发事件: 命中关键词 {', '.join(matched_kws[:5])} — 可能影响组合整体风险敞口",
                    })
                    found_macro += 1
                    print(f"     🌍 Macro news: {headline[:70]}... (keywords: {', '.join(matched_kws[:3])})", flush=True)
                    if found_macro >= 3:
                        break  # Max 3 macro news per run
    except Exception as e:
        print(f"   ⚠️ Macro news scan error: {e}", flush=True)

def check_portfolio_composition(seen_ids, pending):
    """Layer 8: Portfolio concentration alert (daily, 08:00-12:00 only)."""
    # Only run once per day at first invocation after 08:00
    now = datetime.now()
    today_label = now.strftime("%Y-%m-%d")
    daily_key = f"portfolio_comp:{today_label}"
    if daily_key in seen_ids:
        return
    # Only run during morning hours (08:00-12:00) to avoid unnecessary checks
    if now.hour < 8 or now.hour >= 12:
        return
    state = load_portfolio_state()
    if not state:
        return

    # Auto-group holdings by sector using name keywords
    sector_keywords = {
        '半导体/存储': ['美光', '闪迪', 'Lumentum', '中际旭创', '新易盛', '东山精密', '胜宏科技', '生益科技', '东威科技', '杰瑞'],
        'PCB/覆铜板': ['建滔积层板', '中国动力', '思源电气', '紫金黄金'],
        '新能源/电池': ['宁德时代', '工业富联', '信维通信'],
        'AI/算力': ['智谱', 'SK海力士'],
        '光纤光缆': ['长飞光纤'],
        '工业/制造': ['松发股份', '佳鑫国际'],
        '威胜': ['威胜控股'],
    }
    sector_counts = {}
    sector_stocks = {}
    for code, info in state.items():
        name = info.get("name", "")
        assigned = False
        for sector, keywords in sector_keywords.items():
            if any(kw in name for kw in keywords):
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                sector_stocks.setdefault(sector, []).append(f"{code} ({name})")
                assigned = True
                break
        if not assigned:
            sector_counts['其他'] = sector_counts.get('其他', 0) + 1
            sector_stocks.setdefault('其他', []).append(f"{code} ({name})")

    total = len(state)
    alerts = []
    for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        if pct >= 30:
            stocks_str = ', '.join(sector_stocks[sector])
            alerts.append(f"{sector}: {count}/{total} ({pct:.0f}%) — {stocks_str}")

    if alerts:
        pending.append({
            "type": "portfolio_composition",
            "stock": "Portfolio",
            "code": "PORTFOLIO",
            "name": "Portfolio",
            "title": f"持仓集中度预警: {len(alerts)}个行业占比过高",
            "url": "",
            "content": "\\n".join(alerts),
            "source": "Portfolio Analysis",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "reason": f"持仓集中度: {', '.join(a.split(':')[0] for a in alerts)}",
        })
        print(f"     ⚠️ {len(alerts)} concentration alerts", flush=True)
    # Mark as done for the day (whether or not alerts were generated)
    seen_ids.add(daily_key)

# --- Layer 9: Cross-Source News ---
def check_cross_source_news(seen_ids, pending):
    """Layer 9: Cross-reference news from Marketaux + Finnhub market-news.
    Fetches general market news and filters for mentions of portfolio stocks.
    Provides source diversity scoring for materiality.
    """
    state = load_portfolio_state()
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Build search keywords from portfolio
    tickers_us = [code.replace(".O", "") for code in state if code.endswith(".O")]
    names_cn = [info.get("name", "") for info in state.values() if info.get("name")]
    # Add HK stock codes (5-digit zero-padded)
    hk_codes = [code.replace(".HK", "").zfill(5) for code in state if code.endswith(".HK")]
    # Add A-share codes (with .SH/.SZ suffix for matching)
    a_codes = [code for code in state if code.endswith(".SH") or code.endswith(".SZ")]
    # Add sector names for sector-level matching
    try:
        from portfolio_config import SECTOR_MAP
        sector_keywords = list(SECTOR_MAP.keys())  # e.g., '半导体/存储', '光通信/光模块'
    except ImportError:
        sector_keywords = []
    
    # 9a. Finnhub market-news (general, free)
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            items = r.json()
            if isinstance(items, list):
                for item in items[:20]:
                    headline = item.get("headline", "")
                    summary = item.get("summary", "")
                    text = (headline + " " + summary).lower()
                    
                    # Check for portfolio mentions
                    matched = []
                    # US tickers (word boundary to avoid false matches: "MU" → "museum", "LITE" → "elite")
                    for t in tickers_us:
                        if re.search(r'\b' + re.escape(t) + r'\b', text, re.IGNORECASE):
                            matched.append(t)
                    # Full Chinese names (not just first 3 chars)
                    for name in names_cn:
                        if name in (headline + summary):
                            matched.append(name)
                    # HK stock codes (5-digit)
                    for code in hk_codes:
                        if code in text or code.lstrip('0') in text:
                            matched.append(code + ".HK")
                    # A-share codes
                    for code in a_codes:
                        clean = code.replace(".SH", "").replace(".SZ", "")
                        if clean in text:
                            matched.append(code)
                    
                    if not matched:
                        continue
                    
                    mat, reason = is_material(headline)
                    if not mat:
                        continue
                    
                    item_id = f"finnhub_market:{str(item.get('id', ''))}"
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    
                    source = item.get("source", "Market")
                    pub_time = item.get("datetime", 0)
                    time_str = datetime.fromtimestamp(pub_time).strftime("%Y-%m-%d %H:%M") if pub_time else today
                    
                    # Look up name robustly: try exact code, code with suffix, or raw ticker
                    matched_code = matched[0]
                    matched_name = None
                    if matched_code in state:
                        matched_name = state[matched_code].get("name", matched_code)
                    elif matched_code + ".O" in state:
                        matched_name = state[matched_code + ".O"].get("name", matched_code)
                    elif matched_code + ".HK" in state:
                        matched_name = state[matched_code + ".HK"].get("name", matched_code)
                    elif matched_code + ".SH" in state:
                        matched_name = state[matched_code + ".SH"].get("name", matched_code)
                    elif matched_code + ".SZ" in state:
                        matched_name = state[matched_code + ".SZ"].get("name", matched_code)
                    else:
                        matched_name = matched_code
                    
                    pending.append({
                        "type": "cross_source_news",
                        "stock": matched_code,
                        "code": matched_code if "." in matched_code else matched_code,
                        "name": matched_name,
                        "title": f"[Finnhub] {headline[:150]}",
                        "url": item.get("url", ""),
                        "content": summary[:800],
                        "source": source,
                        "time": time_str,
                        "reason": f"市场新闻提及 {', '.join(matched[:3])}",
                        "sources_count": 1,
                    })
    except Exception as e:
        print(f"   Finnhub market news error: {e}", flush=True)
    
    # 9b. Marketaux (rotated ticker batches)
    if MARKETAUX_API_KEY:
        # Cycle through ALL US tickers in batches of 5 to maximize coverage
        # Free tier: 100 req/day. Use 20 for cross-source (5 req × 4 rotations).
        # On each run, rotate which batch gets the widest coverage.
        all_us_tickers = tickers_us
        if not all_us_tickers:
            pass
        else:
            # Rotate: pick a different starting batch each run (hash-based rotation)
            batch_offset = (int(datetime.now().timestamp()) // 3600) % max(1, len(all_us_tickers))
            batches = []
            for i in range(0, len(all_us_tickers), 5):
                batch = all_us_tickers[i:i+5]
                batches.append(batch)
            # Pick 2 batches per run (10 tickers total, uses 2 API calls)
            selected_batches = batches[:2]  # First 2 batches each run
            for batch_idx, batch in enumerate(selected_batches):
                if not batch:
                    continue
                symbols_param = ",".join(batch)
                try:
                    url = (f"https://api.marketaux.com/v1/news/all?symbols={symbols_param}"
                           f"&api_token={MARKETAUX_API_KEY}&limit=3&filter_entities=true"
                           f"&published_after={datetime.now() - timedelta(hours=48):%Y-%m-%dT%H:%M:%S}")
                    r = requests.get(url, timeout=15)
                    if r.status_code == 200:
                        data = r.json()
                        articles = data.get("data", [])
                        for art in articles:
                            headline = art.get("title", "")
                            if not headline:
                                continue
                            mat, reason = is_material(headline)
                            if not mat:
                                continue
                            
                            # Extract which symbols are mentioned in this article
                            entities = art.get("entities", [])
                            mentioned_symbols = [e.get("symbol", "") for e in entities if e.get("symbol") in all_us_tickers]
                            
                            item_id = f"marketaux:{art.get('uuid', '')[:20]}"
                            if item_id in seen_ids:
                                continue
                            seen_ids.add(item_id)
                            
                            # Resolve name from portfolio
                            first_symbol = mentioned_symbols[0] if mentioned_symbols else batch[0]
                            resolved_name = None
                            for suffix in [".O", ".HK", ".SH", ".SZ"]:
                                candidate = first_symbol + suffix
                                if candidate in state:
                                    resolved_name = state[candidate].get("name", first_symbol)
                                    break
                            if not resolved_name:
                                resolved_name = first_symbol
                            
                            pending.append({
                                "type": "cross_source_news",
                                "stock": first_symbol,
                                "code": first_symbol,
                                "name": resolved_name,
                                "title": f"[Marketaux] {headline[:150]}",
                                "url": art.get("url", ""),
                                "content": art.get("description", "")[:800],
                                "source": art.get("source", "Marketaux"),
                                "time": art.get("published_at", today),
                                "reason": f"Marketaux: {reason} (entities: {', '.join(mentioned_symbols[:3])})",
                                "sources_count": 1,
                            })
                        print(f"   Marketaux: {len(articles)} articles for {symbols_param}", flush=True)
                    else:
                        print(f"   Marketaux: HTTP {r.status_code} for {symbols_param}", flush=True)
                except Exception as e:
                    print(f"   Marketaux error for {symbols_param}: {e}", flush=True)
        print(f"   Marketaux: checked {len(selected_batches)} batch(es)", flush=True)
    else:
        print("   Marketaux: no API key (skip)", flush=True)


def load_portfolio_codes_a():
    """Get A-share codes (6-digit) for AKShare calls."""
    state = load_portfolio_state()
    codes = []
    for code in state:
        if code.endswith(".SH"):
            codes.append(code.replace(".SH", ""))
        elif code.endswith(".SZ"):
            codes.append(code.replace(".SZ", ""))
    return codes


# --- SEC CIK Auto-Discovery ---
def get_sec_cik_cache():
    """Auto-discover CIK codes for ALL US portfolio stocks via SEC company_tickers.json.
    Returns {ticker: cik_10digit} dict. Cached daily to ~/.hermes/data/sec_cik_cache.json
    """
    cache = {}
    if SEC_CIK_CACHE_PATH.exists():
        try:
            data = json.loads(SEC_CIK_CACHE_PATH.read_text())
            cached_date = data.get("date", "")
            if cached_date:
                cd = datetime.strptime(cached_date, "%Y-%m-%d")
                if (datetime.now() - cd) < SEC_CIK_UPDATE_INTERVAL:
                    return data.get("ciks", {})
        except:
            pass

    # Fetch SEC company_tickers.json (10,359 entries, updated daily)
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "Portfolio Monitor (portfolio@example.com)"},
            timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            for item in data.values():
                ticker = item.get("ticker", "").upper()
                cik = str(item.get("cik_str", "")).zfill(10)
                if ticker and cik:
                    cache[ticker] = cik

            # Save cache
            SEC_CIK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SEC_CIK_CACHE_PATH.write_text(json.dumps({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "ciks": cache
            }, indent=2))
            print(f"     📋 SEC CIK cache: {len(cache)} tickers loaded", flush=True)
        else:
            print(f"     ⚠️ SEC CIK HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"     ⚠️ SEC CIK fetch error: {e}", flush=True)

    return cache


# --- HK Stock News from AKShare (improved filtering) ---
def get_hk_stock_codes():
    """Get HK stock codes (4-5 digit strings) from portfolio_state.json.
    Returns {padded_code: {code, name}} dict.
    """
    state = load_portfolio_state()
    hk = {}
    for code, info in state.items():
        if code.endswith(".HK"):
            raw = code.replace(".HK", "")
            hk[raw] = {"code": code, "name": info.get("name", raw)}
    return hk

def check_earnings_calendar(seen_ids, pending):
    """Layer 10: HK/A-Share Earnings Calendar — upcoming earnings within 7 days.
    Uses AKShare stock_notice_report for both A-shares and HK stocks.
    Filters for '业绩' or '财报' in announcement title.
    """
    state = load_portfolio_state()
    ak_python = _find_akshare_python()
    if not ak_python:
        return

    today = datetime.now()
    today_str = today.strftime("%Y%m%d")
    week_later = today + timedelta(days=7)

    # === A-Share Earnings Calendar ===
    a_codes = load_portfolio_codes_a()
    if a_codes:
        print(f"   📅 Checking A-share earnings calendar for {len(a_codes)} stocks...", flush=True)
        import subprocess
        script = f"""
import akshare as ak
import json
from datetime import datetime, timedelta

portfolio_codes = {json.dumps(a_codes)}
results = []

try:
    # Fetch upcoming earnings announcements
    df = ak.stock_notice_report(symbol='全部', date='{today_str}')
    if df is not None and not df.empty:
        # Filter for portfolio codes
        df_filtered = df[df['代码'].isin(portfolio_codes)]
        # Filter for earnings-related announcements
        for _, row in df_filtered.iterrows():
            title = str(row.get('公告标题', ''))
            if '业绩' in title or '财报' in title:
                code = row.get('代码', '')
                ann_date = str(row.get('公告日期', ''))
                url = row.get('网址', '')
                ann_type = str(row.get('公告类型', ''))
                results.append({{
                    'code': code,
                    'title': title,
                    'date': ann_date,
                    'url': url,
                    'type': ann_type
                }})
except Exception as e:
    pass

print(json.dumps(results, ensure_ascii=False, default=str))
"""
        try:
            r = subprocess.run([ak_python, "-c", script],
                             capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                earnings_data = json.loads(r.stdout.strip())
                for item in earnings_data:
                    code = item.get('code', '')
                    title = item.get('title', '')
                    ann_date_str = item.get('date', '')
                    url = item.get('url', '')

                    # Build stock code
                    stock_code = code + ".SH" if code.startswith("6") or code.startswith("1") else code + ".SZ"
                    name = state.get(stock_code, {}).get("name", code)

                    # Check if within next 7 days
                    try:
                        for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"]:
                            try:
                                ann_date = datetime.strptime(ann_date_str.split(" ")[0], fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            continue
                        if ann_date < today or ann_date > week_later:
                            continue
                    except:
                        continue

                    # Extract quarter info from title if possible
                    quarter = ""
                    q_match = re.search(r'(\d{4})[年]?(Q\d|第[一二三四]季度|半年度|年度)', title)
                    if q_match:
                        quarter = q_match.group(0)
                    else:
                        quarter = "财报"

                    # Format date as MM-DD
                    date_mmdd = ann_date.strftime("%m-%d")

                    item_id = f"earnings_cal:{stock_code}:{ann_date_str}"
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    full_url = f"https://data.eastmoney.com/notices/detail/{code}/{url}" if url else ""

                    pending.append({
                        "type": "财报预告",
                        "stock": stock_code,
                        "code": stock_code,
                        "name": name,
                        "title": f"财报预告 — {stock_code} ({name})",
                        "url": full_url,
                        "content": f"{quarter}财报预计于{date_mmdd}发布",
                        "source": "东方财富",
                        "time": ann_date_str,
                        "reason": f"财报预告: {quarter}预计于{date_mmdd}发布",
                    })
                    print(f"     📅 {stock_code} ({name}): {quarter} 财报 on {date_mmdd}", flush=True)
        except Exception as e:
            print(f"   ⚠️ A-share earnings calendar error: {e}", flush=True)

    # === HK Earnings Calendar ===
    hk_codes = {}
    for code, info in state.items():
        if code.endswith(".HK"):
            raw = code.replace(".HK", "").zfill(5)
            hk_codes[raw] = {"code": code, "name": info.get("name", "")}

    if hk_codes:
        print(f"   📅 Checking HK earnings calendar for {len(hk_codes)} stocks...", flush=True)
        import subprocess
        hk_code_list = list(hk_codes.keys())
        script = f"""
import akshare as ak
import json

hk_codes = {json.dumps(hk_code_list)}
results = []

try:
    # Use stock_notice_report for HK with proper code format
    for hk_code in hk_codes:
        try:
            df = ak.stock_notice_report(symbol=hk_code, date='{today_str}')
            if df is not None and not df.empty:
                for _, row in df.head(20).iterrows():
                    title = str(row.get('公告标题', ''))
                    if '业绩' in title or '财报' in title or '业绩公告' in title:
                        results.append({{
                            'code': hk_code,
                            'title': title,
                            'date': str(row.get('公告日期', '')),
                            'url': row.get('网址', ''),
                            'type': str(row.get('公告类型', ''))
                        }})
        except:
            pass
except Exception as e:
    pass

print(json.dumps(results, ensure_ascii=False, default=str))
"""
        try:
            r = subprocess.run([ak_python, "-c", script],
                             capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                earnings_data = json.loads(r.stdout.strip())
                for item in earnings_data:
                    hk_raw = item.get('code', '')
                    code_info = hk_codes.get(hk_raw, {})
                    stock_code = code_info.get("code", hk_raw + ".HK")
                    name = code_info.get("name", hk_raw)

                    title = item.get('title', '')
                    ann_date_str = item.get('date', '')
                    url = item.get('url', '')

                    # Check if within next 7 days
                    try:
                        for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"]:
                            try:
                                ann_date = datetime.strptime(ann_date_str.split(" ")[0], fmt)
                                break
                            except ValueError:
                                continue
                        else:
                            continue
                        if ann_date < today or ann_date > week_later:
                            continue
                    except:
                        continue

                    quarter = ""
                    q_match = re.search(r'(\d{4})[年]?(Q\d|第[一二三四]季度|半年度|年度|中期|全年)', title)
                    if q_match:
                        quarter = q_match.group(0)
                    else:
                        quarter = "财报"

                    date_mmdd = ann_date.strftime("%m-%d")

                    item_id = f"earnings_cal_hk:{stock_code}:{ann_date_str}"
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    full_url = f"https://data.eastmoney.com/notices/detail/{hk_raw}/{url}" if url else ""

                    pending.append({
                        "type": "财报预告",
                        "stock": stock_code,
                        "code": stock_code,
                        "name": name,
                        "title": f"财报预告 — {stock_code} ({name})",
                        "url": full_url,
                        "content": f"{quarter}财报预计于{date_mmdd}发布",
                        "source": "东方财富",
                        "time": ann_date_str,
                        "reason": f"财报预告: {quarter}预计于{date_mmdd}发布",
                    })
                    print(f"     📅 {stock_code} ({name}): {quarter} 财报 on {date_mmdd}", flush=True)
        except Exception as e:
            print(f"   ⚠️ HK earnings calendar error: {e}", flush=True)

# --- Main ---
def check_geopolitical_risk(seen_ids, pending):
    """Layer 11: Geopolitical Risk Alerts (地缘政治风险)
    Primary source: RSS feeds (BBC, CNBC, Al Jazeera) with severity-tiered keywords.
    Secondary source: Finnhub general news (fallback).

    Severity thresholds:
    - CRITICAL (1 match → immediate alert)
    - HIGH (2+ matches → alert)
    - NORMAL (3+ matches → alert)
    """
    now = datetime.now()
    today_label = now.strftime("%Y-%m-%d")
    found_events = 0
    MAX_EVENTS_PER_RUN = 3  # Cap to avoid flooding

    # Helper: check headline against severity tiers
    def _check_tier(headline_lower, summary_lower):
        full_text = headline_lower + " " + summary_lower
        matches = {'CRITICAL': [], 'HIGH': [], 'NORMAL': []}
        for tier, keywords in GEOPOLITICAL_TIERS.items():
            for kw in keywords:
                if kw.lower() in full_text:
                    matches[tier].append(kw)
        # Determine if alert-worthy
        if len(matches['CRITICAL']) >= 1:
            return True, 'CRITICAL', matches['CRITICAL'] + matches['HIGH'][:2]
        elif len(matches['HIGH']) >= 2:
            return True, 'HIGH', matches['HIGH'][:3]
        elif len(matches['NORMAL']) >= 3:
            return True, 'NORMAL', matches['NORMAL'][:3]
        return False, None, []

    # Helper: map event to portfolio impact
    def _map_impact(full_text):
        impacted = []
        for event_type, mapping in GEOPOLITICAL_IMPACT_MAP.items():
            if any(kw.lower() in full_text for kw in mapping['keywords']):
                impacted.append({
                    'type': event_type,
                    'description': mapping['description'],
                    'sectors': mapping['sectors'],
                    'stocks': mapping['stocks'],
                })
        return impacted

    # --- 11a. RSS Feed Scan (Primary) ---
    for feed_name, feed_url in RSS_FEEDS:
        if found_events >= MAX_EVENTS_PER_RUN:
            break
        try:
            f = feedparser.parse(feed_url)
            if not f.entries:
                continue
            for entry in f.entries[:8]:  # Check latest 8 from each feed
                title = entry.get('title', '')
                summary = entry.get('summary', '') or entry.get('description', '') or ''
                link = entry.get('link', '')

                # Parse publish time
                pub_time = ''
                for time_field in ['published', 'updated', 'pubDate']:
                    raw = entry.get(time_field, '')
                    if raw:
                        try:
                            dt = parsedate_to_datetime(raw)
                            pub_time = dt.strftime('%Y-%m-%d %H:%M')
                            # Skip if older than 48h
                            if (now - dt).total_seconds() > 172800:
                                break
                        except:
                            pub_time = today_label

                # Check severity tiers
                is_alert, severity, matched_kws = _check_tier(
                    title.lower(), summary.lower()
                )
                if not is_alert:
                    continue

                full_text = (title + " " + summary).lower()
                impact = _map_impact(full_text)

                # Create unique ID
                title_hash = hashlib.md5(title.encode()).hexdigest()[:12]
                item_id = f"geopolitical_rss:{feed_name}:{title_hash}:{today_label}"

                # Check dedup from seen_ids
                if item_id in seen_ids:
                    continue

                # Also check for similar titles already seen today
                similar = False
                for sid in seen_ids:
                    if sid.startswith(f"geopolitical_rss:{feed_name}:") and today_label in sid:
                        similar = True
                        break
                if similar and len(seen_ids) > 5:
                    # Only dedup aggressively if we've already seen many events
                    continue

                seen_ids.add(item_id)
                found_events += 1

                # Build impact sections string
                impact_sections = ', '.join(set(
                    imp['description'] for imp in impact
                )) if impact else '全市场影响'

                pending.append({
                    "type": "地缘政治风险",
                    "code": "GEOPOLITICAL",
                    "name": "地缘政治",
                    "title": f"[{severity}] {title[:150]}",
                    "url": link,
                    "content": summary[:800],
                    "source": feed_name,
                    "time": pub_time,
                    "reason": f"地缘政治风险({severity}): 命中关键词 {', '.join(matched_kws[:5])}. {impact_sections}",
                    "impacted_sectors": list(set(
                        s for imp in impact for s in imp['sectors']
                    )) if impact else ['整体市场'],
                    "severity": severity,
                })
                print(f"     🌍 [{severity}] {feed_name}: {title[:70]}... ({len(matched_kws)} keywords)", flush=True)

        except Exception as e:
            print(f"   ⚠️ RSS feed '{feed_name}' error: {e}", flush=True)

    # --- 11b. Finnhub Geopolitical Scan (Secondary / fallback) ---
    if found_events < MAX_EVENTS_PER_RUN:
        try:
            r = requests.get(
                f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_API_KEY}",
                timeout=10
            )
            if r.status_code == 200:
                articles = r.json()
                for article in articles[:10]:
                    headline = article.get("headline", "").lower()
                    summary = article.get("summary", "").lower()
                    full_text = headline + " " + summary

                    is_alert, severity, matched_kws = _check_tier(headline, summary)
                    if not is_alert:
                        continue

                    source = article.get("source", "Finnhub")
                    url = article.get("url", "")
                    event_title = article.get("headline", "Geopolitical event")

                    impact = _map_impact(full_text)
                    impact_sections = ', '.join(set(
                        imp['description'] for imp in impact
                    )) if impact else '全市场影响'

                    event_id = f"geopolitical_fn:{str(article.get('id', ''))[:12]}:{today_label}"
                    if event_id in seen_ids:
                        continue
                    seen_ids.add(event_id)
                    found_events += 1

                    pending.append({
                        "type": "地缘政治风险",
                        "code": "GEOPOLITICAL",
                        "name": "地缘政治",
                        "title": f"[{severity}] {event_title[:150]}",
                        "url": url,
                        "content": summary[:800],
                        "source": source,
                        "time": now.strftime("%Y-%m-%d %H:%M"),
                        "reason": f"地缘政治风险({severity}) [Finnhub]: 命中关键词 {', '.join(matched_kws[:5])}. {impact_sections}",
                        "impacted_sectors": list(set(
                            s for imp in impact for s in imp['sectors']
                        )) if impact else ['整体市场'],
                        "severity": severity,
                    })
                    print(f"     🌍 [Finnhub/{severity}] {event_title[:70]}...", flush=True)
                    if found_events >= MAX_EVENTS_PER_RUN:
                        break
        except Exception as e:
            print(f"   ⚠️ Finnhub geopolitical error: {e}", flush=True)


def check_finnhub_us_news(seen_ids, pending):
    """Layer 13 (Finnhub Fallback): Per-stock company-news for US holdings.
    This supplements Layer 9 (cross-source) and Layer 1 (per-stock AKShare)
    specifically for US-listed stocks where AKShare has no coverage.
    Uses Finnhub /company-news endpoint (free tier).
    """
    us_stocks = get_us_stocks()
    if not us_stocks:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    ago = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff_ts = (datetime.now() - timedelta(hours=24)).timestamp()
    state = load_portfolio_state()

    print(f"   🇺🇸 Finnhub US news fallback for {len(us_stocks)} stocks...", flush=True)
    found = 0

    for ticker, info in us_stocks.items():
        try:
            url = (f"https://finnhub.io/api/v1/company-news?symbol={ticker}"
                   f"&from={ago}&to={today}&token={FINNHUB_API_KEY}")
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            news_items = r.json()
            if not isinstance(news_items, list):
                continue

            for item in news_items[:8]:
                headline = item.get("headline", "")
                if not headline:
                    continue
                # Freshness filter
                item_dt = item.get("datetime", 0)
                if item_dt and item_dt < cutoff_ts:
                    continue
                # Materiality filter
                mat, reason = is_material(headline)
                if not mat:
                    continue
                news_id = item.get("id", "")
                item_id = f"finnhub_us:{ticker}:{news_id}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                source = item.get("source", "")
                item_url = item.get("url", "")
                code = ticker + ".O"
                name = info.get("name", ticker)
                summary = item.get("summary", "")[:300]

                pending.append({
                    "type": "per_stock_news",
                    "stock": code,
                    "code": code,
                    "name": name,
                    "title": headline[:150],
                    "url": item_url,
                    "content": summary,
                    "source": f"Finnhub ({source})",
                    "time": datetime.fromtimestamp(item_dt).strftime("%Y-%m-%d %H:%M") if item_dt else today,
                    "reason": reason,
                })
                found += 1

            time.sleep(0.4)  # Finnhub rate limit
        except Exception as e:
            print(f"     ⚠️ Finnhub US news error {ticker}: {e}", flush=True)

    if found:
        print(f"     → {found} material US news via Finnhub fallback", flush=True)


# --- Performance timing helpers ---
def _run_layer(name, fn, seen_ids, pending, start_time):
    """Run a detection layer with timing and global timeout check."""
    elapsed = time.time() - start_time
    if elapsed > GLOBAL_TIMEOUT:
        print(f"  ⏰ GLOBAL TIMEOUT at {name} ({elapsed:.1f}s) — skipping remaining layers", flush=True)
        return
    t0 = time.time()
    layer_budget = 40  # max seconds per layer (180s total / ~15 layers)
    try:
        fn(seen_ids, pending)
        layer_time = time.time() - t0
        print(f"  [{name}] {layer_time:.1f}s", flush=True)
        if layer_time > layer_budget:
            print(f"    ⚠️ Layer {name} exceeded budget ({layer_time:.1f}s > {layer_budget}s)", flush=True)
    except Exception as e:
        layer_time = time.time() - t0
        print(f"  [{name}] {layer_time:.1f}s ERROR: {e}", flush=True)


def _save_perf_log(start_time, layer_times):
    """Save performance log for debugging timeouts."""
    try:
        total = time.time() - start_time
        log = {
            "timestamp": datetime.now().isoformat(),
            "total_seconds": round(total, 1),
            "layers": layer_times,
            "pending_count": 0,  # filled by caller
        }
        PERF_LOG_PATH.write_text(json.dumps(log, indent=2, ensure_ascii=False))
    except:
        pass

# ─── HKEX Announcement Monitoring (Layer 14) ───

def _load_hkex_stock_id_cache():
    """Load the HKEX stock ID cache from disk."""
    try:
        if HKEX_STOCK_ID_CACHE_PATH.exists():
            cache = json.loads(HKEX_STOCK_ID_CACHE_PATH.read_text())
            # Purge expired entries
            now = datetime.now().isoformat()
            return {k: v for k, v in cache.items()
                    if v.get("expires", "") > now}
    except Exception as e:
        print(f"  ⚠️ HKEX cache load error: {e}", flush=True)
    return {}

def _save_hkex_stock_id_cache(cache):
    """Save the HKEX stock ID cache to disk."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HKEX_STOCK_ID_CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        print(f"  ⚠️ HKEX cache save error: {e}", flush=True)

def get_hkex_stock_id(stock_code):
    """Look up HKEX stockId for a 5-digit stock code (e.g. '00700').
    Caches results for 7 days to avoid repeated API calls.
    Returns stockId (int) or None.
    """
    cache = _load_hkex_stock_id_cache()
    if stock_code in cache:
        entry = cache[stock_code]
        if entry.get("expires", "") > datetime.now().isoformat():
            return entry.get("stockId")

    try:
        url = f"{HKEX_PREFIX_URL}?callback=callback&lang=EN&type=A&name={stock_code}&market=SEHK"
        text = None
        if SCRAPLING_AVAILABLE:
            try:
                print(f"  🕵️ HKEX prefix: using StealthyFetcher for {stock_code}", flush=True)
                fetcher = StealthyFetcher()
                page = fetcher.fetch(url)
                # For JSONP responses, page.body has raw content without HTML wrapper
                text = page.body.decode('utf-8', errors='replace') if hasattr(page, 'body') and page.body else (page.html_content if hasattr(page, 'html_content') and page.html_content else None)
                if text:
                    print(f"  🕵️ HKEX prefix: StealthyFetcher succeeded for {stock_code}", flush=True)
            except Exception as sf_err:
                print(f"  ⚠️ HKEX prefix: StealthyFetcher failed ({sf_err}), falling back to requests", flush=True)
                text = None

        if text is None:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"  ⚠️ HKEX prefix API returned {r.status_code} for {stock_code}", flush=True)
                return None
            text = r.text

        # Parse JSONP: callback({...});
        text = text.strip()
        json_str = re.sub(r'^callback\(', '', text)
        json_str = re.sub(r'\);?$', '', json_str)
        data = json.loads(json_str)

        stock_info = data.get("stockInfo", [])
        if not stock_info:
            print(f"  ⚠️ HKEX: no stockInfo for {stock_code}", flush=True)
            return None

        sid = stock_info[0].get("stockId")
        if sid is not None:
            expires = (datetime.now() + HKEX_STOCK_ID_CACHE_TTL).isoformat()
            cache[stock_code] = {"stockId": sid, "expires": expires}
            _save_hkex_stock_id_cache(cache)
        return sid

    except Exception as e:
        print(f"  ⚠️ HKEX stockId lookup error {stock_code}: {e}", flush=True)
        return None

def search_hkex_announcements(stock_id, from_date, to_date):
    """Search HKEX announcements for a stock in a date range.
    Args:
        stock_id: HKEX stockId (int)
        from_date: 'YYYYMMDD' string
        to_date: 'YYYYMMDD' string
    Returns list of dicts with: release_time, stock_code, stock_name, title, pdf_link
    """
    try:
        form_data = {
            "lang": "EN",
            "category": "0",
            "market": "SEHK",
            "searchType": "0",
            "documentType": "",
            "t1code": "",
            "t2Gcode": "",
            "t2code": "",
            "stockId": str(stock_id),
            "from": from_date,
            "to": to_date,
            "MB-Daterange": "0",
        }
        html = None
        if SCRAPLING_AVAILABLE:
            try:
                print(f"  🕵️ HKEX search: using StealthyFetcher for stockId={stock_id}", flush=True)
                fetcher = StealthyFetcher()
                page = fetcher.fetch(HKEX_SEARCH_URL + "?lang=en", method="POST", data=form_data)
                html = page.html_content if hasattr(page, 'html_content') and page.html_content else (page.body.decode('utf-8', errors='replace') if hasattr(page, 'body') and page.body else None)
                if html:
                    print(f"  🕵️ HKEX search: StealthyFetcher succeeded for stockId={stock_id}", flush=True)
            except Exception as sf_err:
                print(f"  ⚠️ HKEX search: StealthyFetcher failed ({sf_err}), falling back to requests", flush=True)
                html = None

        if html is None:
            r = requests.post(
                HKEX_SEARCH_URL + "?lang=en",
                data=form_data,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if r.status_code != 200:
                print(f"  ⚠️ HKEX search API returned {r.status_code} for stockId={stock_id}", flush=True)
                return []
            html = r.text
        results = []

        # Parse HTML table rows — extract from each <tr> block
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
        for row in rows:
            try:
                # Split into <td> blocks first
                tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
                if len(tds) < 4:
                    continue

                # TD[0]: release time — text after </span>
                rt_match = re.search(r'</span>\s*([^<]+)', tds[0], re.IGNORECASE | re.DOTALL)
                release_time = rt_match.group(1).strip() if rt_match else ""

                # TD[1]: stock code — text after </span>, before <br/>
                sc_match = re.search(r'</span>\s*([^<]+)', tds[1], re.IGNORECASE | re.DOTALL)
                stock_code = sc_match.group(1).strip() if sc_match else ""

                # TD[2]: stock name — text after </span>, before <br/>
                sn_match = re.search(r'</span>\s*([^<]+)', tds[2], re.IGNORECASE | re.DOTALL)
                stock_name = sn_match.group(1).strip() if sn_match else ""

                # TD[3]: document headline and PDF link
                headline_match = re.search(r'class="headline"[^>]*>\s*(.*?)\s*<', tds[3], re.IGNORECASE | re.DOTALL)
                title = headline_match.group(1).strip() if headline_match else ""

                link_match = re.search(r'<a[^>]*href="(/listedco/[^"]+)"', tds[3], re.IGNORECASE)
                if link_match and title:
                    pdf_link = link_match.group(1).strip()
                else:
                    continue  # Skip rows without links

                if not title:
                    continue

                # Make PDF link absolute
                if pdf_link.startswith("/"):
                    pdf_link = "https://www1.hkexnews.hk" + pdf_link

                results.append({
                    "release_time": release_time,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "title": title,
                    "pdf_link": pdf_link,
                })
            except Exception:
                continue

        return results

    except Exception as e:
        print(f"  ⚠️ HKEX search error stockId={stock_id}: {e}", flush=True)
        return []

def _check_central_bank_speeches(seen_ids, pending):
    """Layer 15: Central Bank Speeches & Unscheduled Remarks.
    Monitors RSS feeds from Fed, ECB, PBOC for speeches, press conferences,
    and unscheduled remarks that can move markets instantly.
    Uses speaker hierarchy + policy keyword matching to filter noise.
    """
    feeds = [
        ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("ECB", "https://www.ecb.europa.eu/press/rss.xml"),
        ("PBOC (via Xinhua)", "http://www.news.cn/fortune/rss.xml"),
    ]

    # --- Speaker Hierarchy (named entity matching) ---
    # High-impact: Chair/President — speeches ALWAYS move markets
    SPEAKER_HIGH = [
        "Powell", "Lagarde", "Pan Gongsheng", "潘功胜",
        "Williams", "Barr", "Jefferson", "Kugler", "Bowman", "Waller",
        "Schnabel", "Lane",
    ]
    # Medium-impact: Regional Fed presidents — move markets on policy topics
    SPEAKER_MEDIUM = [
        "Daly", "Musalem", "Hammack", "Collins", "Bostic", "Jefferson",
        "Goolsbee", "Logan", "Schmid",
    ]

    # --- Policy Topic Keywords (market-moving vs. routine) ---
    POLICY_HIGH = [
        "rate decision", "rate hike", "rate cut", "fomc", "monetary policy",
        "quantitative", "tightening", "easing", "balance sheet", "dot plot",
        "interest rate", "forward guidance", "inflation outlook",
        "加息", "降息", "利率", "货币政策", "量化宽松", "量化紧缩",
        "准备金率", "降准", "LPR", "MLF",
    ]
    POLICY_MEDIUM = [
        "economic outlook", "financial stability", "employment", "labor market",
        "housing", "credit", "banking", "regulation", "stress test",
        "经济展望", "金融稳定", "就业", "信贷",
    ]

    # General speech keywords (require speaker match to pass)
    SPEECH_KEYWORDS = [
        "speech", "press conference", "remarks", "testimony", "statement",
        "讲话", "新闻发布会",
    ]

    now = datetime.now()
    cutoff = now - timedelta(hours=12)

    found = 0
    for source_name, feed_url in feeds:
        try:
            d = feedparser.parse(feed_url)
            if not d.entries:
                continue
            for entry in d.entries[:15]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")[:300]
                pub_time = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    if pub_dt < cutoff:
                        continue
                    pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")
                elif hasattr(entry, "published") and entry.published:
                    try:
                        pub_dt = parsedate_to_datetime(entry.published)
                        if pub_dt.tzinfo:
                            pub_dt = pub_dt.replace(tzinfo=None)
                        if pub_dt < cutoff:
                            continue
                        pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pub_time = now.strftime("%Y-%m-%d %H:%M")

                text = f"{title} {summary}"
                text_lower = text.lower()

                # --- Named Entity + Policy Matching ---
                speakers_found = []
                for s in SPEAKER_HIGH:
                    if s.lower() in text_lower:
                        speakers_found.append((s, "high"))
                if not speakers_found:
                    for s in SPEAKER_MEDIUM:
                        if s.lower() in text_lower:
                            speakers_found.append((s, "medium"))

                policy_high = [kw for kw in POLICY_HIGH if kw.lower() in text_lower]
                policy_med = [kw for kw in POLICY_MEDIUM if kw.lower() in text_lower]
                speech_kw = [kw for kw in SPEECH_KEYWORDS if kw.lower() in text_lower]

                # Materiality gate:
                # 1. High-impact speaker + any keyword → fire
                # 2. Any speaker + high-impact policy keyword → fire
                # 3. High-impact policy keyword alone → fire (covers unscheduled events)
                # 4. Medium speaker + medium policy → fire
                is_material_speech = False
                impact_level = "LOW"

                if any(level == "high" for _, level in speakers_found) and (policy_high or policy_med or speech_kw):
                    is_material_speech = True
                    impact_level = "HIGH"
                elif speakers_found and policy_high:
                    is_material_speech = True
                    impact_level = "HIGH"
                elif policy_high:
                    is_material_speech = True
                    impact_level = "HIGH"
                elif any(level == "medium" for _, level in speakers_found) and (policy_high or policy_med):
                    is_material_speech = True
                    impact_level = "MEDIUM"

                if not is_material_speech:
                    continue

                # Build reason string
                speaker_names = [s for s, _ in speakers_found] if speakers_found else ["unspecified"]
                matched_kws = policy_high + policy_med[:2]
                reason_parts = []
                if speakers_found:
                    reason_parts.append(f"讲话人: {', '.join(speaker_names)}")
                if matched_kws:
                    reason_parts.append(f"政策关键词: {', '.join(matched_kws[:3])}")
                reason = f"央行讲话/声明 ({impact_level}) | {'; '.join(reason_parts)}"

                item_id = f"cb_speech:{source_name}:{title[:50]}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                link = entry.get("link", "")
                pending.append({
                    "type": "central_bank_speech",
                    "stock": "MACRO",
                    "code": "MACRO",
                    "name": source_name,
                    "title": title[:200],
                    "url": link,
                    "content": summary,
                    "source": source_name,
                    "time": pub_time,
                    "reason": reason,
                    "event_type": "macro_event",
                    "impact_level": impact_level,
                })
                found += 1
                print(f"     🏦 [{source_name}] [{impact_level}] {title[:80]}...", flush=True)
                if found >= 5:
                    break
        except Exception as e:
            print(f"     ⚠️ Central bank feed error {source_name}: {e}", flush=True)

    if found:
        print(f"     → {found} central bank events", flush=True)


def _check_insider_trading(seen_ids, pending):
    """Layer 16: SEC Form 4 Insider Trading Monitor.
    Tracks insider buys/sells for US portfolio holdings via EDGAR RSS.
    Large insider purchases are often early signals of positive developments.
    """
    us_stocks = get_us_stocks()
    if not us_stocks:
        return

    # EDGAR Form 4 RSS feed (updated every ~15 min)
    rss_url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=&type=4&dateb=&owner=include&count=100&search_text=&action=getcompany&output=atom"

    try:
        import requests
        headers = {
            "User-Agent": "Orange-Hermes-Agent (orange@example.com)",
            "Accept-Encoding": "gzip, deflate",
        }
        r = requests.get(rss_url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"     ⚠️ EDGAR Form 4 RSS returned {r.status_code}", flush=True)
            return

        d = feedparser.parse(r.content)
        if not d.entries:
            return

        cutoff = datetime.now() - timedelta(hours=12)
        found = 0

        # Build CIK lookup from portfolio
        cik_map = {}
        for ticker, info in us_stocks.items():
            cik = info.get("cik")
            if cik:
                cik_map[str(cik).zfill(10)] = ticker

        for entry in d.entries[:50]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")

            # Extract CIK from link or title
            cik_match = re.search(r'CIK=(\d+)', link) or re.search(r'(\d{10})', title)
            if not cik_match:
                continue
            cik = cik_match.group(1).zfill(10)

            if cik not in cik_map:
                continue

            ticker = cik_map[cik]
            info = us_stocks[ticker]

            # Parse time
            pub_time = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_dt = datetime(*entry.published_parsed[:6])
                if pub_dt < cutoff:
                    continue
                pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")

            # Detect buy vs sell
            is_buy = bool(re.search(r'buy|purchase|award|grant', title, re.IGNORECASE))
            is_sell = bool(re.search(r'sale|sell|dispose', title, re.IGNORECASE))
            direction = "买入" if is_buy else "卖出" if is_sell else "未知"

            # Extract transaction details from summary
            shares_match = re.search(r'(\d[\d,]*)\s*shares', summary, re.IGNORECASE)
            price_match = re.search(r'\$\s*([\d.]+)', summary)
            owner_match = re.search(r'(?:by|of)\s+([A-Z][a-zA-Z\s\.\']{2,30})\s*(?:Chief|Director|President|CEO|CFO|COO|CTO)', summary)

            shares = shares_match.group(1) if shares_match else "N/A"
            price = f"${price_match.group(1)}" if price_match else "N/A"
            insider = owner_match.group(1).strip() if owner_match else "Unknown"

            # Skip small transactions (<1000 shares)
            try:
                share_num = int(shares.replace(",", ""))
                if share_num < 1000:
                    continue
            except (ValueError, AttributeError):
                pass  # Don't skip if can't parse

            item_id = f"form4:{cik}:{title[:40]}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            code = ticker + ".O"
            name = info.get("name", ticker)

            pending.append({
                "type": "insider_trading",
                "stock": code,
                "code": code,
                "name": name,
                "title": f"{name} ({ticker}) — 内部人{direction} {shares}股 @ {price}",
                "url": link,
                "content": f"交易方向: {direction} | 股数: {shares} | 价格: {price} | 内部人: {insider}",
                "source": "SEC Form 4 (EDGAR)",
                "time": pub_time,
                "reason": f"内部人交易: {insider} {direction} {shares}股",
                "event_type": "insider_activity",
            })
            found += 1
            print(f"     📊 [Form 4] {ticker}: {insider} {direction} {shares}股", flush=True)
            if found >= 5:
                break

        if found:
            print(f"     → {found} insider trading events", flush=True)

    except Exception as e:
        print(f"     ⚠️ Form 4 insider trading error: {e}", flush=True)


def _check_financial_press(seen_ids, pending):
    """Layer 17: Tier-1 Financial Press Headlines.
    Monitors Bloomberg, WSJ, FT RSS feeds for market-moving headlines.
    These sources often break stories before they hit general news aggregators.
    """
    feeds = [
        ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
        ("WSJ Markets", "https://feeds.a.wsj.com/market_data/news"),
        ("FT Markets", "https://www.ft.com/rss/home"),
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ]

    # Fallback feeds that are known to work
    working_feeds = [
        ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
        ("Bloomberg", "https://www.bloomberg.com/feeds/markets"),
    ]

    market_keywords = [
        "china", "hong kong", "fed", "rate", "trade", "tariff",
        "sanction", "export control", "semiconductor", "ai", "tech",
        "gold", "currency", "yuan", "renminbi", "oil", "commodity",
        "earnings", "revenue", "profit", "loss", "merger", "acquisition",
        "ipo", "bankruptcy", "default", "debt", "inflation", "recession",
        "geopolitical", "war", "conflict", "election", "policy",
        "中国", "香港", "美联储", "利率", "关税", "贸易", "半导体",
        "通胀", "经济", "央行", "黄金", "汇率",
    ]

    now = datetime.now()
    cutoff = now - timedelta(hours=12)

    found = 0
    for source_name, feed_url in working_feeds:
        try:
            d = feedparser.parse(feed_url)
            if not d.entries:
                continue
            for entry in d.entries[:15]:
                title = entry.get("title", "")
                if not title:
                    continue
                summary = entry.get("summary", "")[:300]

                # Time filter
                pub_time = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    if pub_dt < cutoff:
                        continue
                    pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")
                elif hasattr(entry, "published") and entry.published:
                    try:
                        pub_dt = parsedate_to_datetime(entry.published)
                        if pub_dt.tzinfo:
                            pub_dt = pub_dt.replace(tzinfo=None)
                        if pub_dt < cutoff:
                            continue
                        pub_time = pub_dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pub_time = now.strftime("%Y-%m-%d %H:%M")

                # Keyword match
                text = f"{title} {summary}".lower()
                matched = [kw.lower() for kw in market_keywords if kw.lower() in text]
                if not matched:
                    continue

                item_id = f"fin_press:{source_name}:{title[:60]}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                link = entry.get("link", "")

                pending.append({
                    "type": "financial_press",
                    "stock": "MACRO",
                    "code": "MACRO",
                    "name": source_name,
                    "title": title[:200],
                    "url": link,
                    "content": summary,
                    "source": source_name,
                    "time": pub_time,
                    "reason": f"一线财经媒体: 命中关键词 {', '.join(matched[:3])}",
                    "event_type": "market_news",
                })
                found += 1
                print(f"     📰 [{source_name}] {title[:80]}...", flush=True)
                if found >= 5:
                    break
        except Exception as e:
            print(f"     ⚠️ Financial press error {source_name}: {e}", flush=True)

    if found:
        print(f"     → {found} tier-1 press headlines", flush=True)


def check_hkex_announcements(seen_ids, pending):
    """Layer 14: HKEX disclosure API monitoring for HK portfolio stocks."""
    # 1. Load HK stocks from portfolio — merge HK_STOCK_CODES and SECTOR_MAP .HK codes
    hk_stocks = dict(HK_STOCK_CODES)  # start with static mapping

    try:
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from portfolio_config import SECTOR_MAP
        for sector, codes in SECTOR_MAP.items():
            for code in codes:
                if code.endswith(".HK"):
                    # Strip .HK and zero-pad to 5 digits
                    num = code.replace(".HK", "")
                    padded = num.zfill(5)
                    if padded not in hk_stocks:
                        hk_stocks[padded] = code  # use portfolio code as name fallback
    except Exception as e:
        print(f"  ⚠️ HKEX: could not load SECTOR_MAP: {e}", flush=True)

    if not hk_stocks:
        print("  ℹ️  No HK stocks found — skipping HKEX monitoring", flush=True)
        return

    now = datetime.now()
    from_date = (now - timedelta(days=7)).strftime("%Y%m%d")
    to_date = now.strftime("%Y%m%d")

    for hk_code, name in hk_stocks.items():
        try:
            # a. Get stockId (cached)
            stock_id = get_hkex_stock_id(hk_code)
            if not stock_id:
                print(f"  ℹ️  HKEX: no stockId for {hk_code}", flush=True)
                continue

            # b. Search last 7 days of announcements
            anns = search_hkex_announcements(stock_id, from_date, to_date)

            # Portfolio code format: strip leading zeros + .HK
            portfolio_code = hk_code.lstrip("0") + ".HK"

            count = 0
            for ann in anns:
                if count >= 5:
                    break  # Max 5 announcements per stock per run

                title = ann.get("title", "")
                pdf_link = ann.get("pdf_link", "")
                release_time = ann.get("release_time", "")
                stock_name = ann.get("stock_name", name)

                # c. Filter for material events
                mat, reason = is_material(title)
                if not mat:
                    continue

                # d. Check seen_ids to avoid duplicates
                item_id = f"hkex:{hk_code}:{pdf_link[-30:]}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                # Parse release time: "09/04/2026 18:34" → "2026-04-09 18:34"
                time_str = ""
                try:
                    dt = datetime.strptime(release_time, "%d/%m/%Y %H:%M")
                    time_str = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    time_str = release_time

                # Use portfolio code for stock/code fields
                display_name = HK_STOCK_CODES.get(hk_code, stock_name)

                # f. Append to pending list
                pending.append({
                    "type": "hkex_announcement",
                    "stock": portfolio_code,
                    "code": portfolio_code,
                    "name": display_name,
                    "title": title,
                    "url": pdf_link,
                    "content": f"Release time: {release_time}",
                    "source": "HKEX",
                    "time": time_str,
                    "reason": reason,
                })
                count += 1

            if count > 0:
                print(f"  📋 HKEX: {count} material announcements for {hk_code}", flush=True)

            time.sleep(0.3)  # Rate limit between stocks

        except Exception as e:
            print(f"  ⚠️ HKEX error {hk_code}: {e}", flush=True)
            continue

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose progress output")
    parser.add_argument("--verbose", action="store_true", help="Force verbose output even to pipe")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord delivery (for LLM-driven cron jobs)")
    parser.add_argument("--report-delivery", action="store_true", help="DEPRECATED: LLM now handles all Discord delivery")
    parser.add_argument("--report", action="store_true", help="Print pending events summary to stdout (for LLM analysis)")
    args = parser.parse_args()

    # Default: quiet (safe for cron), --verbose to force output in terminal
    # Only noisy when explicitly requested
    quiet = True  # default to silent; manual runs use --verbose
    if args.verbose:
        quiet = False  # --verbose overrides

    if not quiet:
        print(f"=== Portfolio News Monitor v4 @ {datetime.now().isoformat()} ===", flush=True)
    state = load_portfolio_state()
    if not quiet:
        print(f"  📋 Portfolio: {len(state)} positions", flush=True)

    history = load_news_history()
    seen_ids = set(history.get("seen", []))
    pending = []
    start_time = time.time()
    layer_times = {}

    # Layer 1: Per-stock news (A-share + HK)
    t = time.time(); _run_layer("L1:Per-stock", check_per_stock_news, seen_ids, pending, start_time); layer_times["L1"] = round(time.time()-t, 1)
    # Layer 2: SEC 8-K
    t = time.time(); _run_layer("L2:SEC-8K", check_sec_filings, seen_ids, pending, start_time); layer_times["L2"] = round(time.time()-t, 1)
    # Layer 3: Earnings
    t = time.time(); _run_layer("L3:Earnings", check_earnings, seen_ids, pending, start_time); layer_times["L3"] = round(time.time()-t, 1)

    # ── HIGH PRIORITY: run L14-L17 early before timeout consumes budget ──
    # Layer 14: HKEX announcements
    t = time.time(); _run_layer("L14:HKEX", check_hkex_announcements, seen_ids, pending, start_time); layer_times["L14"] = round(time.time()-t, 1)
    # Layer 15: Central Bank Speeches
    t = time.time(); _run_layer("L15:CentralBank", _check_central_bank_speeches, seen_ids, pending, start_time); layer_times["L15"] = round(time.time()-t, 1)
    # Layer 16: Insider Trading
    t = time.time(); _run_layer("L16:Insider", _check_insider_trading, seen_ids, pending, start_time); layer_times["L16"] = round(time.time()-t, 1)
    # Layer 17: Financial Press
    t = time.time(); _run_layer("L17:FinPress", _check_financial_press, seen_ids, pending, start_time); layer_times["L17"] = round(time.time()-t, 1)

    # ── Standard layers (lower priority) ──
    # Layer 4: A-share announcements
    t = time.time(); _run_layer("L4:Announcements", check_ashare_announcements, seen_ids, pending, start_time); layer_times["L4"] = round(time.time()-t, 1)
    # Layer 5: Competitor events
    t = time.time(); _run_layer("L5:Competitors", check_competitor_events, seen_ids, pending, start_time); layer_times["L5"] = round(time.time()-t, 1)
    # Layer 6: Commodity prices
    t = time.time(); _run_layer("L6:Commodities", check_commodity_prices, seen_ids, pending, start_time); layer_times["L6"] = round(time.time()-t, 1)
    # Layer 7: Macro events
    t = time.time(); _run_layer("L7:Macro", check_macro_events, seen_ids, pending, start_time); layer_times["L7"] = round(time.time()-t, 1)
    # Layer 8: Portfolio composition
    t = time.time(); _run_layer("L8:Portfolio", check_portfolio_composition, seen_ids, pending, start_time); layer_times["L8"] = round(time.time()-t, 1)
    # Layer 9: Cross-source news
    t = time.time(); _run_layer("L9:Cross-source", check_cross_source_news, seen_ids, pending, start_time); layer_times["L9"] = round(time.time()-t, 1)
    # Layer 10: HK/A-Share Earnings Calendar
    t = time.time(); _run_layer("L10:Earnings-cal", check_earnings_calendar, seen_ids, pending, start_time); layer_times["L10"] = round(time.time()-t, 1)
    # Layer 11: Geopolitical Risk
    t = time.time(); _run_layer("L11:Geopolitical", check_geopolitical_risk, seen_ids, pending, start_time); layer_times["L11"] = round(time.time()-t, 1)
    # Layer 13: Finnhub US stock news fallback
    t = time.time(); _run_layer("L13:Finnhub-US", check_finnhub_us_news, seen_ids, pending, start_time); layer_times["L13"] = round(time.time()-t, 1)

    # --- Classify event types ---
    for event in pending:
        event["event_type"] = classify_event_type(event)

    # Save state
    history["seen"] = sorted(seen_ids)
    save_news_history(history)

    # Write pending events
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if pending:
        PENDING_NEWS_PATH.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
        if not quiet:
            print(f"  🚨 {len(pending)} material events → {PENDING_NEWS_PATH.name}", flush=True)

        # Event type summary
        type_counts = {}
        for ev in pending:
            et = ev.get("event_type", "unknown")
            type_counts[et] = type_counts.get(et, 0) + 1
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(type_counts.items()))
        if not quiet:
            print(f"  📊 Event types: {summary}", flush=True)

        # Send directly to Discord (unless --no-discord flag set for LLM-driven jobs)
        if not args.no_discord:
            send_to_discord(pending)
    else:
        if PENDING_NEWS_PATH.exists():
            PENDING_NEWS_PATH.unlink()
        if not quiet:
            print("  ✅ No material events", flush=True)

    if not quiet:
        print(f"  📊 Dedup entries: {len(seen_ids)}", flush=True)

    # Save performance log
    layer_times["_total"] = round(time.time() - start_time, 1)
    _save_perf_log(start_time, layer_times)

    # --report: print pending events summary for LLM analysis
    if args.report:
        if pending:
            print(f"\nPENDING_EVENTS_COUNT: {len(pending)}", flush=True)
            for i, ev in enumerate(pending, 1):
                print(f"\nEVENT_{i}:", flush=True)
                print(f"  type: {ev.get('type', '')}", flush=True)
                print(f"  event_type: {ev.get('event_type', '')}", flush=True)
                print(f"  severity: {ev.get('severity', '')}", flush=True)
                print(f"  title: {ev.get('title', '')}", flush=True)
                print(f"  code: {ev.get('code', '')}", flush=True)
                print(f"  time: {ev.get('time', '')}", flush=True)
                print(f"  source: {ev.get('source', '')}", flush=True)
                print(f"  url: {ev.get('url', '')}", flush=True)
                print(f"  reason: {ev.get('reason', '')}", flush=True)
                content = ev.get('content', '')
                if content:
                    print(f"  content: {content}", flush=True)
                impacted = ev.get('impacted_sectors', [])
                if impacted:
                    print(f"  impacted_sectors: {', '.join(impacted)}", flush=True)
        else:
            print("\nPENDING_EVENTS_COUNT: 0", flush=True)
            print("NO_MATERIAL_EVENTS", flush=True)

if __name__ == "__main__":
    main()
