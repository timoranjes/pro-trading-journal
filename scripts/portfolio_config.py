# Auto-generated sector mapping - DO NOT EDIT MANUALLY
# Run: python3 auto_sector_classify.py --update to regenerate
import json
import requests
from pathlib import Path

# === Configuration ===
DISCORD_CHANNEL = "1502241038579011655"  # #portfolio-alerts (restored channel)
WEBHOOK_CACHE_PATH = Path.home() / ".hermes" / "data" / "webhooks.json"
ALERT_THRESHOLD = 0.07  # 7%

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


def get_stock_sector(code):
    """Return the sector for a stock code."""
    for sector, codes in SECTOR_MAP.items():
        if code in codes:
            return sector
    return None


def sina_to_code(sina_symbol):
    """Convert Sina symbol to portfolio code format."""
    if sina_symbol.startswith('gb_'):
        return sina_symbol.replace('gb_', '').upper() + '.O'
    elif sina_symbol.startswith('rt_hk') or sina_symbol.startswith('hk'):
        prefix = 'rt_hk' if sina_symbol.startswith('rt_hk') else 'hk'
        code = sina_symbol.replace(prefix, '').lstrip('0') + '.HK'
        return code
    else:
        if sina_symbol.startswith('sh'):
            return sina_symbol.replace('sh', '') + '.SH'
        elif sina_symbol.startswith('sz'):
            return sina_symbol.replace('sz', '') + '.SZ'
    return sina_symbol


def get_webhook_url(channel_id):
    """Load webhook URL from cache."""
    try:
        if WEBHOOK_CACHE_PATH.exists():
            cache = json.loads(WEBHOOK_CACHE_PATH.read_text())
            return cache.get(channel_id)
    except Exception as e:
        print(f"  Webhook cache error: {e}", flush=True)
    return None


def send_discord(msg, mention_here=False):
    """Send message via webhook (no bot token needed).
    
    Args:
        msg: Message content
        mention_here: If True, prepend @here to the message
    """
    webhook_url = get_webhook_url(DISCORD_CHANNEL)
    if not webhook_url:
        print("  No webhook URL found for channel, skipping alert.", flush=True)
        return
    
    # Prepend @here if requested
    if mention_here:
        msg = "@here " + msg
    
    try:
        r = requests.post(webhook_url, json={"content": msg}, timeout=10)
        if r.status_code in (200, 204):
            print("  Alert sent to Discord via webhook", flush=True)
        else:
            print(f"  Webhook error: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"  Webhook network error: {e}", flush=True)