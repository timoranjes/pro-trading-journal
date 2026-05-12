#!/usr/bin/env python3
"""
Weekly Trader Brief Generator — Sina + Yahoo + AKShare (no Wind dependency)

Data sources:
  A-share indices: Sina Finance (hq.sinajs.cn)
  HK indices: Sina Finance
  US indices: Yahoo Finance v8
  Sectors: AKShare (A-share + HK industry boards)
  VIX: Yahoo Finance (current + 20-day average + term structure proxy)
  Southbound flow: AKShare (stock_hsgt_fund_flow_summary_em)
  Northbound flow: AKShare (沪股通+深股通 net flows)
  Cross-Asset: Yahoo Finance v8 (USD/CNH, Gold, US10Y, DXY, Silver, Oil)
  HK Short Interest: AKShare (graceful fallback to N/A)
  CICC Flows: ~/.hermes/cron/output/cicc_flows.txt cache
  Portfolio holdings: portfolio_config.py SECTOR_MAP

Output: Strict DATA block → LLM may only ADD analysis, NEVER modify numbers.

Enhanced 6-dimension trader brief:
  1. Flow & Positioning Signals (南向/北向资金、CICC、沽空比率)
  2. Technical Levels (关键支撑/阻力、整数关口)
  3. Regime & Liquidity Detection (VIX结构、相关性断裂)
  4. Catalyst Timing & Priced-In Assessment (财报、政策、数据)
  5. Execution Intelligence (波动率窗口、流动性评估)
  6. Cross-Asset Macro Translation (CNH、美债、黄金→股市含义)
"""

import sys
import os
import requests
from datetime import date, timedelta, datetime

sys.stdout.reconfigure(line_buffering=True)

# ── Config ────────────────────────────────────────────────────

SINA_URL = "http://hq.sinajs.cn/list="
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2wk&interval=1d"
YAHOO_CHART_1M_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
CICC_FLOWS_PATH = os.path.expanduser("~/.hermes/cron/output/cicc_flows.txt")

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn"
}

# ── Date helpers ───────────────────────────────────────────────

def get_week_dates():
    """Return (monday_iso, friday_iso) for the most recent completed trading week."""
    today = date.today()
    wd = today.weekday()
    if wd == 6:        # Sunday
        friday = today - timedelta(days=2)
    elif wd == 0:      # Monday
        friday = today - timedelta(days=3)
    elif wd == 5:      # Saturday
        friday = today - timedelta(days=1)
    else:
        friday = today - timedelta(days=wd - 4)
    monday = friday - timedelta(days=4)
    return monday.isoformat(), friday.isoformat()


# ── Sina Finance (A-share + HK) ───────────────────────────────

SINA_INDICES = {
    '上证指数':   'sh000001',
    '沪深300':    'sh000300',
    '深证成指':   'sz399001',
    '创业板指':   'sz399006',
    '恒生指数':   'rt_hsi',
    '恒生科技':   'rt_hstech',
}

def sina_fetch(indices_dict):
    """Fetch indices from Sina Finance. Returns {name: (close, pct_chg)}."""
    results = {}
    try:
        symbols = ','.join(indices_dict.values())
        r = requests.get(f"{SINA_URL}{symbols}", headers=UA, timeout=10)
        r.encoding = 'gbk'
        for line in r.text.split('\n'):
            if not line.strip():
                continue
            sym = line.split('hq_str_')[-1].split('="')[0]
            data = line.split('="')[-1].split('"')[0].split(',')
            if len(data) < 30:
                continue

            name = None
            for n, s in indices_dict.items():
                if s == sym:
                    name = n
                    break
            if not name:
                continue

            try:
                close = float(data[3])  # 现价
                prev_close = float(data[4])  # 昨收
                if prev_close > 0:
                    pct = (close - prev_close) / prev_close * 100
                    results[name] = (round(close, 2), round(pct, 2))
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f'[Sina] Fetch error: {e}', flush=True)
    return results


# ── Yahoo Finance (US indices) ────────────────────────────────

YAHOO_INDICES = [
    ('标普500',   '^GSPC'),
    ('纳斯达克',  '^IXIC'),
    ('道琼斯',    '^DJI'),
    ('VIX',       '^VIX'),
]

def yahoo_weekly_change(sym):
    """Get week-over-week change from Yahoo Finance."""
    try:
        r = requests.get(YAHOO_CHART_URL.format(sym=sym), headers=UA, timeout=8)
        if r.status_code != 200:
            return None
        res = r.json()['chart']['result'][0]
        quotes = res.get('indicators', {}).get('quote', [{}])[0]
        closes = [c for c in quotes.get('close', []) if c is not None]
        if len(closes) >= 6:
            return (round(closes[-1], 2),
                    round((closes[-1] - closes[-6]) / closes[-6] * 100, 2))
        elif len(closes) >= 2:
            return (round(closes[-1], 2),
                    round((closes[-1] - closes[0]) / closes[0] * 100, 2))
        return None
    except Exception:
        return None


# ── Cross-Asset Dashboard ─────────────────────────────────────

CROSS_ASSET_SYMBOLS = [
    ('USD/CNH',  'USDCNH=X'),
    ('黄金',      'GC=F'),
    ('白银',      'SI=F'),
    ('原油',      'CL=F'),
    ('美10Y收益率', '^TNX'),
    ('美元指数',   'DX-Y.NYB'),
]

def fetch_cross_asset():
    """Fetch cross-asset data: USD/CNH, Gold, US10Y, DXY."""
    results = {}
    for name, sym in CROSS_ASSET_SYMBOLS:
        d = yahoo_weekly_change(sym)
        if d:
            results[name] = d
    return results


# ── VIX Context (current vs 20-day average) ───────────────────

def fetch_vix_context():
    """Fetch VIX level and 20-day moving average for context."""
    try:
        r = requests.get(YAHOO_CHART_1M_URL.format(sym='^VIX'), headers=UA, timeout=8)
        if r.status_code != 200:
            return None, None
        res = r.json()['chart']['result'][0]
        quotes = res.get('indicators', {}).get('quote', [{}])[0]
        closes = [c for c in quotes.get('close', []) if c is not None]
        if not closes:
            return None, None
        current = round(closes[-1], 2)
        avg20 = round(sum(closes[-20:]) / len(closes[-20:]), 2) if len(closes) >= 5 else None
        return current, avg20
    except Exception:
        return None, None


# ── HK Short Interest (AKShare) ───────────────────────────────

def fetch_hk_short_interest():
    """Try to fetch HK short interest / margin data from AKShare.

    Known AKShare functions for HK margin data are limited.
    Try stock_hk_summary_margin or similar; if unavailable, return None gracefully.
    """
    try:
        import akshare as ak
        # Try the most likely candidate; AKShare API may change
        if hasattr(ak, 'stock_hk_summary_margin'):
            df = ak.stock_hk_summary_margin()
            if df is not None and not df.empty:
                return df.to_string()
        # Fallback: no suitable function found
        return None
    except Exception:
        return None


# ── CICC Flows Cache ──────────────────────────────────────────

def read_cicc_flows_cache():
    """Read last 500 chars of CICC flows cache file, if it exists."""
    try:
        if os.path.exists(CICC_FLOWS_PATH):
            with open(CICC_FLOWS_PATH, 'r', encoding='utf-8') as f:
                content = f.read()
            return content[-500:] if len(content) > 500 else content
    except Exception:
        pass
    return None


# ── AKShare Sectors (if available) ────────────────────────────

def akshare_sectors():
    """Get A-share sector performance from AKShare."""
    results = {}
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = row.get('板块名称', '')
                pct = row.get('涨跌幅', 0)
                if name and pct is not None:
                    try:
                        results[name] = round(float(pct), 2)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return results


def akshare_hk_sectors():
    """Get HK sector performance from AKShare."""
    results = {}
    try:
        import akshare as ak
        df = ak.stock_hk_board_industry_em()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = row.get('板块名称', '')
                pct = row.get('涨跌幅', 0)
                if name and pct is not None:
                    try:
                        results[name] = round(float(pct), 2)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return results


def akshare_southbound():
    """Get southbound (港股通) net flow from AKShare."""
    results = {}
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is not None and not df.empty:
            # Filter for 南向 (southbound) only
            south = df[df['资金方向'] == '南向']
            if not south.empty:
                latest_date = south['交易日'].max()
                day_data = south[south['交易日'] == latest_date]
                for _, row in day_data.iterrows():
                    plate = row.get('板块', '')
                    net = row.get('成交净买额', 0)
                    if plate and net is not None:
                        try:
                            val = float(net)
                            if '港股通(沪)' in plate:
                                results['沪港通南向'] = round(val, 2)  # Already in 亿元
                            elif '港股通(深)' in plate:
                                results['深港通南向'] = round(val, 2)  # Already in 亿元
                        except (ValueError, TypeError):
                            pass
            # Filter for 北向 (northbound) too
            north = df[df['资金方向'] == '北向']
            if not north.empty:
                latest_date = north['交易日'].max()
                day_data = north[north['交易日'] == latest_date]
                for _, row in day_data.iterrows():
                    plate = row.get('板块', '')
                    net = row.get('成交净买额', 0)
                    if plate and net is not None:
                        try:
                            val = float(net)
                            if '沪股通' in plate:
                                results['沪股通北向'] = round(val, 2)
                            elif '深股通' in plate:
                                results['深股通北向'] = round(val, 2)
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass
    return results


# ── Brief builder ──────────────────────────────────────────────

WEEK_NUM_MAP = ['一', '二', '三', '四', '五', '六', '日']


def build_brief(mon, fri, indices, sectors, hk_sectors,
                vix_current, vix_avg20, southbound,
                cross_asset, hk_short, cicc_flows):
    mon_dt = date.fromisoformat(mon)
    fri_dt = date.fromisoformat(fri)
    mon_label = f'{mon_dt.month}/{mon_dt.day} ({WEEK_NUM_MAP[mon_dt.weekday()]})'
    fri_label = f'{fri_dt.month}/{fri_dt.day} ({WEEK_NUM_MAP[fri_dt.weekday()]})'

    lines = []
    lines.append('<!-- WEEKLY_TRADER_BRIEF: period={}~{} -->'.format(mon, fri))
    lines.append('')
    lines.append('## ====== RAW DATA BLOCK (DO NOT MODIFY NUMBERS BELOW) ======')
    lines.append('')

    # ── 1. Index Performance ──
    lines.append('### [DATA] 指数表现')
    lines.append('| 指数 | 收盘 | 周涨跌% |')
    lines.append('|------|------|---------|')
    for name, (close, chg) in indices.items():
        lines.append(f'| {name} | {close:,.2f} | {chg:+.2f}% |')
    lines.append('')

    # ── 2. Cross-Asset Dashboard ──
    lines.append('### [DATA] 跨资产仪表盘')
    if cross_asset:
        lines.append('| 资产 | 最新价 | 周涨跌% |')
        lines.append('|------|--------|---------|')
        for name, (close, chg) in cross_asset.items():
            lines.append(f'| {name} | {close:,.2f} | {chg:+.2f}% |')
    else:
        lines.append('跨资产数据: N/A (Yahoo不可用)')
    lines.append('')

    # ── 3. VIX Context ──
    lines.append('### [DATA] VIX波动率')
    if vix_current is not None:
        label = '低波动' if vix_current < 15 else ('中等' if vix_current < 25 else '高波动/恐慌')
        lines.append(f'- VIX当前: {vix_current:.2f} ({label})')
        if vix_avg20 is not None:
            diff = vix_current - vix_avg20
            direction = '高于' if diff > 0 else '低于'
            lines.append(f'- VIX 20日均值: {vix_avg20:.2f} (当前{direction}均值 {diff:+.2f})')
        else:
            lines.append('- VIX 20日均值: N/A')
    else:
        lines.append('- VIX: N/A')
    lines.append('')

    # ── 4. Sector Rotation ──
    lines.append('### [DATA] 板块轮动')
    if sectors:
        sorted_s = sorted(sectors.items(), key=lambda x: x[1], reverse=True)
        lines.append('**A股行业 (AKShare):**')
        for name, pct in sorted_s[:15]:
            lines.append(f'- {name}: {pct:+.2f}%')
        top3 = ', '.join(s[0] for s in sorted_s[:3])
        bot3 = ', '.join(s[0] for s in sorted_s[-3:])
        lines.append(f'领涨前三: {top3}')
        lines.append(f'垫底前三: {bot3}')
    else:
        lines.append('A股行业: N/A')
    lines.append('')

    if hk_sectors:
        sorted_hk = sorted(hk_sectors.items(), key=lambda x: x[1], reverse=True)
        lines.append('**港股行业 (AKShare):**')
        for name, pct in sorted_hk[:10]:
            lines.append(f'- {name}: {pct:+.2f}%')
    else:
        lines.append('港股行业: N/A')
    lines.append('')

    # ── 5. Fund Flow ──
    lines.append('### [DATA] 资金流')
    if southbound:
        lines.append('**港股通（南向）:**')
        if '沪港通南向' in southbound:
            lines.append(f'- 沪: {southbound["沪港通南向"]:+.2f} 亿')
        if '深港通南向' in southbound:
            lines.append(f'- 深: {southbound["深港通南向"]:+.2f} 亿')
        total_south = 0.0
        if '沪港通南向' in southbound:
            total_south += southbound['沪港通南向']
        if '深港通南向' in southbound:
            total_south += southbound['深港通南向']
        if total_south != 0.0:
            lines.append(f'- 南向合计: {total_south:+.2f} 亿')
        # Northbound
        north_items = {k: v for k, v in southbound.items() if '北向' in k}
        if north_items:
            lines.append('')
            lines.append('**陆股通（北向）:**')
            if '沪股通北向' in north_items:
                lines.append(f'- 沪: {north_items["沪股通北向"]:+.2f} 亿')
            if '深股通北向' in north_items:
                lines.append(f'- 深: {north_items["深股通北向"]:+.2f} 亿')
            total_north = sum(north_items.values())
            if total_north != 0.0:
                lines.append(f'- 北向合计: {total_north:+.2f} 亿')
    else:
        lines.append('南北向资金: N/A')
    lines.append('')

    # ── 6. HK Short Interest ──
    lines.append('### [DATA] 港股沽空/融资')
    if hk_short:
        lines.append('```')
        lines.append(hk_short.strip())
        lines.append('```')
    else:
        lines.append('港股沽空数据: N/A (AKShare暂无对应接口)')
    lines.append('')

    # ── 7. CICC Flows Reference ──
    lines.append('### [DATA] 中金资金流参考')
    if cicc_flows:
        lines.append('```')
        lines.append(cicc_flows.strip())
        lines.append('```')
    else:
        lines.append('中金资金流: N/A (缓存文件不存在)')
    lines.append('')

    lines.append('## ====== END RAW DATA BLOCK ======')
    lines.append('')

    # ── LLM ANALYSIS SECTIONS (LLM fills these below) ──
    lines.append('---')
    lines.append('')
    lines.append('## 📋 LLM ANALYSIS TASK')
    lines.append('')
    lines.append('> ⚠️ **CRITICAL RULES:**')
    lines.append('> 1. You MUST NOT modify any numbers in the [DATA] block above.')
    lines.append('> 2. All conclusions MUST be based ONLY on the DATA block numbers + web_search for news.')
    lines.append('> 3. When citing news, include source URL and date.')
    lines.append('> 4. Write ALL analysis sections below in **简体中文**.')
    lines.append('> 5. NEVER hallucinate data — only reference what is in the DATA block or found via web_search.')
    lines.append('> 6. Each section should be concise (3-5 bullets max). Focus on actionable insights for a PM.')
    lines.append('')

    lines.append('### 1️⃣ Flow & Positioning Signals (资金流与仓位信号)')
    lines.append('> 基于DATA区块中的南向/北向资金、CICC资金流、沽空数据，分析：')
    lines.append('> - 外资对A股和港股的态度（北向资金趋势 vs 南向资金趋势对比）')
    lines.append('> - 是否有拥挤交易警告（某板块连续大幅净流入 + 沽空比率异常低 = 拥挤多头）')
    lines.append('> - ETF申赎信号：如果跨资产中黄金/美债与股市出现背离，可能暗示机构在对冲或调仓')
    lines.append('> **PM关注点：** 当前市场是加仓还是减仓？资金在流向什么、流出什么？')
    lines.append('')

    lines.append('### 2️⃣ Regime & Liquidity Detection (体制与流动性)')
    lines.append('> 基于DATA区块中的VIX水平（vs 20日均值）、跨资产变动，分析：')
    lines.append('> - 当前波动率体制：低波动(<15)/正常(15-25)/高波动(>25)，与上周相比是升级还是降级')
    lines.append('> - 跨资产相关性是否异常：正常情况下USD/CNH涨→港股承压，如果打破此关系说明有结构性变化')
    lines.append('> - 黄金与VIX是否同步：如果两者同时大幅上涨，暗示smart money在系统性对冲')
    lines.append('> **PM关注点：** 当前市场环境是否支持现有仓位？流动性是在改善还是恶化？')
    lines.append('')

    lines.append('### 3️⃣ Technical Levels That Matter (关键技术位)')
    lines.append('> 基于DATA区块中的指数收盘价，指出：')
    lines.append('> - 关键整数关口和前期高低点（恒生指数20000/25000、标普5000/5500等）')
    lines.append('> - 当前价格距离关键技术位的百分比（越近越值得关注）')
    lines.append('> - 如果跌破关键位可能触发程序化卖盘的区域')
    lines.append('> **注意：** 不得编造DATA中不存在的价格数据。可通过web_search补充关键技术位，但须注明来源。')
    lines.append('> **PM关注点：** 哪些技术位被突破会改变市场结构？')
    lines.append('')

    lines.append('### 4️⃣ Catalyst Timing & "Priced-In" Assessment (催化剂与定价评估)')
    lines.append('> 通过web_search获取：')
    lines.append('> - 下周重要经济数据发布（非农、CPI、PMI等）')
    lines.append('> - 央行会议/政策动向（FOMC、PBOC LPR等）')
    lines.append('> - 重要财报发布（尤其是 portfolio 中的相关公司）')
    lines.append('> 结合DATA区块中的VIX和跨资产变动，判断市场是否已充分定价这些催化剂。')
    lines.append('> **PM关注点：** 哪些催化剂已经被定价？哪些可能带来惊喜/惊吓？')
    lines.append('')

    lines.append('### 5️⃣ Execution Intelligence (交易执行建议)')
    lines.append('> 基于DATA区块中的波动率和流动性指标，给出下周交易执行建议：')
    lines.append('> - 最佳交易时间窗口：如果VIX较高/波动加剧，建议避免开盘前30分钟交易')
    lines.append('> - 流动性评估：如果南向/北向资金大幅波动，说明市场流动性不稳定，大单需拆分')
    lines.append('> - 跨市场机会：如果A股和港股同板块出现显著价差，是否存在套利/调仓机会')
    lines.append('> **PM关注点：** 如果要调整仓位，什么时候执行冲击最小？')
    lines.append('')

    lines.append('### 6️⃣ Cross-Asset Macro Translation (跨资产宏观解读)')
    lines.append('> 基于DATA区块中的USD/CNH、美债收益率、美元指数、黄金、原油变动，翻译为股市含义：')
    lines.append('> - CNH走贬 vs 走稳：对港股（尤其是内地收入占比高的公司）的影响')
    lines.append('> - 美债收益率变动：对成长股估值的影响（收益率↑→成长股估值承压）')
    lines.append('> - 美元指数的含义：DXY走强通常意味着新兴市场资金外流压力')
    lines.append('> - 黄金+原油组合：黄金涨+原油跌 = 避险情绪；黄金涨+原油涨 = 通胀预期')
    lines.append('> **PM关注点：** 宏观信号是否在支持或威胁现有仓位方向？')
    lines.append('')

    lines.append('### 📅 Week Ahead Summary (下周总结)')
    lines.append('> 用3-5句话总结本周市场变化核心逻辑，以及下周需要关注的最重要1-2个风险/机会。')
    lines.append('> 格式："本周核心逻辑是...。下周重点关注...，如果...则需要..."')
    lines.append('')

    lines.append('---')
    lines.append(f'_Generated by Hermes (Sina + Yahoo + AKShare + CICC) | {datetime.now().strftime("%Y-%m-%d %H:%M HKT")}_')

    return '\n'.join(lines)


# ── Main ───────────────────────────────────────────────────────

def main():
    mon, fri = get_week_dates()

    indices = {}
    sectors = {}
    hk_sectors = {}
    vix_current = None
    vix_avg20 = None
    southbound = {}
    cross_asset = {}
    hk_short = None
    cicc_flows = None

    # 1. Fetch A-share + HK indices from Sina
    print('[Sina] Fetching indices...', flush=True)
    sina_data = sina_fetch(SINA_INDICES)
    indices.update(sina_data)

    # 2. Fetch US indices from Yahoo
    print('[Yahoo] Fetching US indices...', flush=True)
    for name, sym in YAHOO_INDICES:
        d = yahoo_weekly_change(sym)
        if d:
            if name == 'VIX':
                vix_current = d[0]
            else:
                indices[name] = d

    # 3. Fetch VIX context (current vs 20-day average)
    print('[Yahoo] Fetching VIX context...', flush=True)
    vix_current, vix_avg20 = fetch_vix_context()

    # 4. Fetch cross-asset dashboard
    print('[Yahoo] Fetching cross-asset data...', flush=True)
    cross_asset = fetch_cross_asset()

    # 5. Fetch sectors from AKShare
    print('[AKShare] Fetching sectors...', flush=True)
    sectors = akshare_sectors()
    hk_sectors = akshare_hk_sectors()

    # 6. Fetch southbound flow
    print('[AKShare] Fetching southbound flow...', flush=True)
    southbound = akshare_southbound()

    # 7. Fetch HK short interest (graceful skip)
    print('[AKShare] Fetching HK short interest...', flush=True)
    hk_short = fetch_hk_short_interest()

    # 8. Read CICC flows cache
    print('[CICC] Reading flows cache...', flush=True)
    cicc_flows = read_cicc_flows_cache()

    # Print brief
    brief = build_brief(mon, fri, indices, sectors, hk_sectors,
                        vix_current, vix_avg20, southbound,
                        cross_asset, hk_short, cicc_flows)
    print(brief)


if __name__ == '__main__':
    main()
