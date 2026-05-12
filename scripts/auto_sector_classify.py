#!/usr/bin/env python3
"""
Auto Sector Classification v3 — Uses AkShare (CN/HK) + Yahoo Finance (US) for industry data.
Updates portfolio_config.py SECTOR_MAP dynamically based on current holdings.

Wind API removed — no longer in use. Sources: AkShare (A-share/HK), Yahoo Finance (US).

Usage:
    python3 auto_sector_classify.py [--update]

With --update: Writes new SECTOR_MAP to portfolio_config.py
Without --update: Prints proposed changes for review
"""
import sys
import json
import re
import warnings
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings('ignore')

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False
    print("⚠️  AkShare not available, falling back to manual mapping")

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("⚠️  yfinance not available, US stocks will use manual mapping")

sys.stdout.reconfigure(line_buffering=True)

CONFIG_PATH = Path.home() / ".hermes" / "scripts" / "portfolio_config.py"
MONITOR_PATH = Path.home() / ".hermes" / "scripts" / "portfolio_monitor.py"

# Manual fallback mapping (verified from company research)
MANUAL_SECTOR_MAP = {
    # US Stocks
    'MU.O': '半导体/存储',  # Micron - Memory
    'SNDK.O': '半导体/存储',  # SanDisk - Storage
    'LITE.O': '光通信/光模块',  # Lumentum - Optical
    
    # HK Stocks
    '1888.HK': 'PCB/覆铜板',  # 建滔积层板 - CCL
    '6869.HK': '光纤光缆',  # 长飞光纤 - Fiber optic
    '7709.HK': 'AI/算力/云',  # 南方东英 2 倍做多 SK 海力士 ETF - AI/Cloud ETF
    '3200.HK': '工业/制造',  # 大族数控 - CNC equipment
    '3393.HK': '威胜',  # 威胜控股 - Energy metering
    '2513.HK': 'AI/算力/云',  # 智谱 - AI
    '3858.HK': '材料/化工',  # 佳鑫国际资源 - 鎢矿 (Tungsten mining)
    '2259.HK': '黄金/贵金属',  # 紫金黄金国际 - Gold
    
    # A-Share Stocks
    '002353.SZ': '工业/制造',  # 杰瑞股份 - Oilfield services
    '300308.SZ': '光通信/光模块',  # 中际旭创 - Optical modules
    '300502.SZ': '光通信/光模块',  # 新易盛 - Optical modules
    '600176.SH': '材料/玻纤',  # 中国巨石 - Fiberglass
    '300750.SZ': '新能源/电池',  # 宁德时代 - Battery (CATL)
    '002384.SZ': 'PCB/覆铜板',  # 东山精密 - PCB/FPC
    '601138.SH': 'AI/算力/云',  # 工业富联 - Cloud/Server
    '3200.HK': 'PCB/覆铜板',  # 大族数控 - PCB equipment
    '300476.SZ': 'PCB/覆铜板',  # 胜宏科技 - PCB
    '002028.SZ': '电力设备',  # 思源电气 - Power distribution
    '600183.SH': 'PCB/覆铜板',  # 生益科技 - CCL
    '603268.SH': '工业/制造',  # 松发股份 - 船舶制造 (2025 重组后转型)
    '600482.SH': '工业/制造',  # 中国动力 - Power equipment
    '688700.SH': 'PCB/覆铜板',  # 东威科技 - PCB plating equipment
    '300136.SZ': '射频/天线',  # 信维通信 - RF/Antenna
    '688808.SH': '工业/制造',  # 联讯仪器 - 测试仪器 (Test equipment)
    '300438.SZ': '新能源/电池',  # 鹏辉能源 - Battery
}

# AkShare industry to our sector mapping
AKSHARE_SECTOR_MAP = {
    '半导体': '半导体/存储',
    '元件': '半导体/存储',
    '光学光电子': '光通信/光模块',
    '通信设备': '光通信/光模块',
    '消费电子': '消费电子',
    '印制电路板': 'PCB/覆铜板',
    '电子化学品': 'PCB/覆铜板',
    '电池': '新能源/电池',
    '光伏设备': '新能源/光伏',
    '光纤': '光纤光缆',
    '云计算': 'AI/算力/云',
    '数据中心': 'AI/算力/云',
    '黄金': '黄金/贵金属',
    '贵金属': '黄金/贵金属',
    '生物制品': '医疗/生物医药',
    '医药': '医疗/生物医药',
    '专用设备': '工业/制造',
    '机械设备': '工业/制造',
    '电网设备': '电力设备',
    '电气': '电力设备',
    '仪器仪表': '威胜',
    '化工': '材料/化工',
    '玻纤': '材料/玻纤',
    '陶瓷': '材料/陶瓷',
}


def get_yahoo_sector(code):
    """Get sector/industry from Yahoo Finance for US stocks."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        # Convert 'MU.O' → 'MU', 'SNDK.O' → 'SNDK'
        ticker = code.replace('.O', '')
        stock = yf.Ticker(ticker)
        info = stock.info
        # Try multiple fields
        for field in ['industry', 'sector']:
            val = info.get(field)
            if val and val != 'N/A':
                return str(val).strip()
    except Exception:
        pass
    return None


def get_akshare_industry_cn(code):
    """Get industry classification from AkShare for CN/HK stocks."""
    try:
        if code.endswith('.SZ') or code.endswith('.SH'):
            # A-share: use stock info
            stock_code = code.replace('.SZ', '').replace('.SH', '')
            df = ak.stock_individual_info_em(symbol=stock_code)
            if '行业' in df.columns:
                industry = df['行业'].iloc[0] if len(df) > 0 else None
                if industry:
                    return str(industry).strip()
        elif code.endswith('.HK'):
            # HK stock: try to get info from eastmoney
            try:
                stock_code = code.replace('.HK', '')
                df = ak.stock_individual_info_em(symbol=stock_code)
                if '行业' in df.columns:
                    industry = df['行业'].iloc[0] if len(df) > 0 else None
                    if industry:
                        return str(industry).strip()
            except Exception:
                pass
    except Exception as e:
        pass
    return None


def classify_stock(code, name):
    """
    Classify a single stock into a sector.
    Priority: 1) Manual mapping 2) AkShare (CN/HK) 3) Yahoo Finance (US) 4) Name keyword matching
    Returns (sector, confidence, source)
    """
    # Priority 1: Manual mapping (most reliable)
    if code in MANUAL_SECTOR_MAP:
        return MANUAL_SECTOR_MAP[code], 'high', 'manual'
    
    # Priority 2: AkShare API for CN/HK stocks
    if AKSHARE_AVAILABLE and (code.endswith('.SZ') or code.endswith('.SH') or code.endswith('.HK')):
        industry = get_akshare_industry_cn(code)
        if industry:
            for keyword, sector in AKSHARE_SECTOR_MAP.items():
                if keyword in industry:
                    return sector, 'medium', f'akshare:{industry}'
    
    # Priority 3: Yahoo Finance for US stocks
    if YFINANCE_AVAILABLE and code.endswith('.O'):
        industry = get_yahoo_sector(code)
        if industry:
            # Map Yahoo industry names to our sectors
            for keyword, sector in AKSHARE_SECTOR_MAP.items():
                if keyword in industry:
                    return sector, 'medium', f'yahoo:{industry}'
    
    # Priority 4: Name keyword matching
    name_keywords = {
        '光通信': '光通信/光模块',
        '光电': '光通信/光模块',
        '激光': '光通信/光模块',
        '存储': '半导体/存储',
        '芯片': '半导体/存储',
        '半导': '半导体/存储',
        'PCB': 'PCB/覆铜板',
        '覆铜': 'PCB/覆铜板',
        '电路': 'PCB/覆铜板',
        '电镀': 'PCB/覆铜板',
        '电池': '新能源/电池',
        '锂': '新能源/电池',
        '储': '新能源/电池',
        '光纤': '光纤光缆',
        '光缆': '光纤光缆',
        '云': 'AI/算力/云',
        '算力': 'AI/算力/云',
        '数据': 'AI/算力/云',
        '智能': 'AI/算力/云',
        '黄金': '黄金/贵金属',
        '金': '黄金/贵金属',
        '矿': '黄金/贵金属',
        '生物': '医疗/生物医药',
        '医药': '医疗/生物医药',
        '医疗': '医疗/生物医药',
        '诊断': '医疗/生物医药',
        '动力': '工业/制造',
        '机械': '工业/制造',
        '设备': '工业/制造',
        '油': '工业/制造',
        '数控': '工业/制造',
        '仪器': '工业/制造',
        '电力': '电力设备',
        '电气': '电力设备',
        '电网': '电力设备',
        '分销': 'IT 分销',
        '商贸': 'IT 分销',
        '威': '威胜',
        '计量': '威胜',
        '仪表': '威胜',
        'ETF': 'AI/算力/云',
        '基金': '其他',
        '玻纤': '材料/玻纤',
        '玻璃': '材料/玻纤',
        '陶瓷': '材料/陶瓷',
        '化工': '材料/化工',
    }
    
    for keyword, sector in name_keywords.items():
        if keyword in name:
            return sector, 'medium', f'name_keyword:{keyword}'
    
    return '其他', 'low', 'fallback'


def build_sector_map(portfolio_state):
    """Build SECTOR_MAP from portfolio holdings."""
    sector_map = defaultdict(list)
    
    print("\n=== Auto Sector Classification ===\n")
    
    for code, info in sorted(portfolio_state.items()):
        name = info.get('name', 'Unknown')
        sector, confidence, source = classify_stock(code, name)
        sector_map[sector].append(code)
        print(f"  ✅ {code} ({name}) → {sector} (confidence: {confidence}, source: {source})")
    
    # Sort sectors and codes
    result = {}
    for sector in sorted(sector_map.keys()):
        result[sector] = sorted(sector_map[sector])
    
    return result


def read_current_config():
    """Read current SECTOR_MAP from portfolio_config.py."""
    if not CONFIG_PATH.exists():
        return None
    
    content = CONFIG_PATH.read_text()
    import re
    match = re.search(r'SECTOR_MAP\s*=\s*(\{[^}]+\})', content, re.DOTALL)
    if match:
        try:
            sector_map = eval(match.group(1))
            return sector_map
        except:
            return None
    return None


def generate_config_code(sector_map):
    """Generate Python code for SECTOR_MAP with comments."""
    lines = ["# Auto-generated sector mapping - DO NOT EDIT MANUALLY",
             "# Run: python3 auto_sector_classify.py --update to regenerate",
             "SECTOR_MAP = {"]
    for sector, codes in sector_map.items():
        codes_str = ', '.join(f"'{c}'" for c in codes)
        lines.append(f"    '{sector}': [{codes_str}],")
    lines.append("}")
    return '\n'.join(lines)


def extract_holdings_from_config():
    """Extract all stock codes from SECTOR_MAP in portfolio_config.py."""
    if not CONFIG_PATH.exists():
        return {}
    content = CONFIG_PATH.read_text()
    match = re.search(r'SECTOR_MAP\s*=\s*(\{[^}]+\})', content, re.DOTALL)
    if not match:
        return {}
    try:
        sector_map = eval(match.group(1))
    except Exception:
        return {}
    # Flatten: {code: {'name': '', 'sector': sector} for each code}
    holdings = {}
    for sector, codes in sector_map.items():
        for code in codes:
            holdings[code] = {'name': '', 'sector': sector}
    return holdings


def main():
    # Read holdings from SECTOR_MAP (not portfolio_state.json — Wind API removed)
    portfolio_state = extract_holdings_from_config()
    if not portfolio_state:
        print("❌ No holdings found in portfolio_config.py SECTOR_MAP")
        sys.exit(1)
    
    print(f"📊 Found {len(portfolio_state)} stocks in portfolio SECTOR_MAP\n")
    
    # Build new sector map
    new_sector_map = build_sector_map(portfolio_state)
    
    print("\n=== Proposed SECTOR_MAP ===\n")
    for sector, codes in new_sector_map.items():
        print(f"{sector}: {', '.join(codes)}")
    
    # Compare with current
    current_map = read_current_config()
    if current_map:
        print("\n=== Changes ===\n")
        all_sectors = set(new_sector_map.keys()) | set(current_map.keys())
        for sector in sorted(all_sectors):
            old_codes = set(current_map.get(sector, []))
            new_codes = set(new_sector_map.get(sector, []))
            if old_codes != new_codes:
                if sector not in current_map:
                    print(f"➕ NEW: {sector} = {sorted(new_codes)}")
                elif sector not in new_sector_map:
                    print(f"➖ REMOVED: {sector} = {sorted(old_codes)}")
                else:
                    added = new_codes - old_codes
                    removed = old_codes - new_codes
                    if added:
                        print(f"+ {sector}: +{sorted(added)}")
                    if removed:
                        print(f"- {sector}: -{sorted(removed)}")
    else:
        print("\n⚠️  No existing SECTOR_MAP found")
    
    # Update config if --update flag
    if '--update' in sys.argv:
        print("\n=== Writing to config files ===\n")
        new_code = generate_config_code(new_sector_map)
        
        # Update portfolio_config.py
        if CONFIG_PATH.exists():
            config_content = CONFIG_PATH.read_text()
            import re
            updated_content = re.sub(
                r'#.*Auto-generated.*?\nSECTOR_MAP\s*=\s*\{[^}]+\}',
                new_code,
                config_content,
                flags=re.DOTALL
            )
            CONFIG_PATH.write_text(updated_content)
            print("✅ SECTOR_MAP updated in portfolio_config.py")
        
        # Update portfolio_monitor.py
        if MONITOR_PATH.exists():
            monitor_content = MONITOR_PATH.read_text()
            import re
            if re.search(r'SECTOR_MAP\s*=\s*\{', monitor_content):
                updated_monitor = re.sub(
                    r'#.*Auto-generated.*?\nSECTOR_MAP\s*=\s*\{[^}]+\}',
                    new_code,
                    monitor_content,
                    flags=re.DOTALL
                )
                # If no auto-generated comment, replace the whole block
                if '# Auto-generated' not in updated_monitor:
                    updated_monitor = re.sub(
                        r'SECTOR_MAP\s*=\s*\{[^}]+\}',
                        new_code,
                        monitor_content,
                        flags=re.DOTALL
                    )
                MONITOR_PATH.write_text(updated_monitor)
                print("✅ SECTOR_MAP updated in portfolio_monitor.py")
        
        print("\n💡 Tip: Run this script after adding new stocks to portfolio")
    else:
        print("\n💡 Run with --update flag to write changes")


if __name__ == "__main__":
    main()
