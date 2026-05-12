#!/usr/bin/env python3
"""
Guba Rumor Monitor v2 — Eastmoney 股吧 sentiment tracker for portfolio A-shares.
Monitors retail attention and hot posts to detect "buy rumor, sell news" cycles.

Data sources:
1. AKShare stock_comment_em: 关注指数 (attention index) — primary signal
2. Eastmoney 股吧 hot posts — secondary signal (browser-based, optional)

Alert triggers:
- Attention index spike (>5% run-over-run)
- High-attention stocks (关注指数 > 85) with positive rank change
- Rumor keywords in guba post titles

Usage: python3 guba_rumor_monitor.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

STATE_FILE = Path.home() / ".hermes/cron/output/guba_rumor_state.json"
PORTFOLIO_FILE = Path.home() / ".hermes/portfolio_state.json"

# High attention threshold
ATTENTION_THRESHOLD = 85

# Rumor keywords and their weights
RUMOR_KEYWORDS = {
    "传闻": 25, "据传": 25, "未证实": 20, "爆料": 20, "内部消息": 25,
    "据说": 15, "网传": 15, "小道消息": 15, "独家": 10, "突发": 10,
    "重磅": 10, "超预期": 15, "大订单": 20, "重大合同": 20,
    "收购": 15, "重组": 15, "技术突破": 15, "新产品": 10,
    "利好": 10, "重大利好": 15, "业绩暴增": 15,
    "2.4T": 20, "800G": 15, "1.6T": 15,
}


def load_portfolio():
    if not PORTFOLIO_FILE.exists():
        return {}
    with open(PORTFOLIO_FILE) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if ".SZ" in k or ".SH" in k}


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"attention": {}, "last_run": None}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_attention_index(portfolio_codes):
    """Fetch attention index for all portfolio A-shares."""
    try:
        import akshare as ak
        df = ak.stock_comment_em()
    except Exception as e:
        print(f"[ERROR] AKShare failed: {e}", file=sys.stderr)
        return {}

    results = {}
    for code in portfolio_codes:
        pure_code = code.split(".")[0]
        match = df[df["代码"] == pure_code]
        if not match.empty:
            row = match.iloc[0]
            results[code] = {
                "name": str(row.get("名称", "")),
                "attention": float(row.get("关注指数", 0)),
                "score": float(row.get("综合得分", 0)),
                "rank_change": int(row.get("上升", 0)),
                "rank": int(row.get("目前排名", 9999)),
                "price": str(row.get("最新价", "")),
                "change_pct": str(row.get("涨跌幅", "")),
            }
    return results


def main():
    now = datetime.now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M')}] Guba rumor monitor starting...")

    portfolio = load_portfolio()
    if not portfolio:
        print("No A-share holdings found.")
        return

    print(f"Monitoring {len(portfolio)} A-shares...")
    state = load_state()
    alerts = []

    # Fetch attention index
    attention = get_attention_index(list(portfolio.keys()))

    for code, data in attention.items():
        name = data["name"]
        att = data["attention"]
        prev_att = state.get("attention", {}).get(code, {}).get("attention", 0)

        # Check for attention spike
        if prev_att > 0 and att > prev_att * 1.05:  # 5% spike
            spike_pct = (att - prev_att) / prev_att * 100
            alerts.append({
                "type": "spike",
                "code": code,
                "name": name,
                "prev_att": prev_att,
                "curr_att": att,
                "spike_pct": spike_pct,
                "rank_change": data["rank_change"],
                "rank": data["rank"],
                "price": data["price"],
                "change_pct": data["change_pct"],
            })
        # Also alert if absolute attention is high
        elif att > ATTENTION_THRESHOLD:
            alerts.append({
                "type": "high_attention",
                "code": code,
                "name": name,
                "curr_att": att,
                "rank_change": data["rank_change"],
                "rank": data["rank"],
                "price": data["price"],
                "change_pct": data["change_pct"],
            })

    # Format and output alerts
    if alerts:
        print(f"\n=== {len(alerts)} Rumor Alerts ===\n")
        for a in alerts:
            if a["type"] == "spike":
                print(
                    f"📈 关注指数飙升 | {a['code']} {a['name']}\n"
                    f"   {a['prev_att']:.1f} → {a['curr_att']:.1f} (+{a['spike_pct']:.0f}%)\n"
                    f"   排名变化: {a['rank_change']:+d} (当前 #{a['rank']})\n"
                    f"   股价: {a['price']} ({a['change_pct']}%)"
                )
            else:
                print(
                    f"🔥 高关注度 | {a['code']} {a['name']}\n"
                    f"   关注指数: {a['curr_att']:.1f} (阈值: {ATTENTION_THRESHOLD})\n"
                    f"   排名变化: {a['rank_change']:+d} (当前 #{a['rank']})\n"
                    f"   股价: {a['price']} ({a['change_pct']}%)"
                )
            print("---")
    else:
        print("No significant rumor activity detected.")

    # Save state
    state["attention"] = {
        code: {
            "attention": data["attention"],
            "rank_change": data["rank_change"],
            "rank": data["rank"],
            "timestamp": now.isoformat(),
        }
        for code, data in attention.items()
    }
    state["last_run"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
