#!/usr/bin/env python3
"""
Macro Economic Calendar Tracker v2.1 — Fixed business-day scheduling.
  Sources: Finnhub API + hardcoded schedule for major recurring events.

Key fixes over v2:
  - ISM Manufacturing = FIRST BUSINESS DAY of month (not Monday)
  - ISM Services = THIRD BUSINESS DAY of month (not first Wednesday)
  - China PMI = last day of PREVIOUS month
  - FOMC decision time = 2 PM ET → next day 2 AM HKT
  - Empty-date Finnhub events filtered out
  - Today's past events flagged correctly
"""

import json
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

import requests

sys.stdout.reconfigure(line_buffering=True)

FINNHUB_KEY = "d6n74gpr01qir35jdoagd6n74gpr01qir35jdob0"
DATA_DIR = Path.home() / ".hermes" / "data"
CALENDAR_PATH = DATA_DIR / "macro_calendar.json"

# FOMC meetings 2026
FOMC_MEETINGS_2026 = [
    ("2026-01-28", "2026-01-29"),  # 2-day meeting ends Jan 28
    ("2026-03-18", "2026-03-19"),
    ("2026-05-06", "2026-05-07"),  # May 6 (Wed) 2 PM ET = May 7 (Thu) 2 AM HKT
    ("2026-06-17", "2026-06-18"),
    ("2026-07-29", "2026-07-30"),
    ("2026-09-16", "2026-09-17"),
    ("2026-11-04", "2026-11-05"),
    ("2026-12-16", "2026-12-17"),
]

# PBOC LPR — 每月20日 (or next business day)
PBOC_LPR_2026 = [
    "2026-01-20", "2026-02-20", "2026-03-20", "2026-04-20",
    "2026-05-20", "2026-06-22", "2026-07-20", "2026-08-20",
    "2026-09-21", "2026-10-20", "2026-11-20", "2026-12-21",
]


def _nth_business_day(y: int, m: int, nth: int) -> date:
    """Get the Nth business day (Mon-Fri) of a month (1-indexed)."""
    d = date(y, m, 1)
    count = 0
    while d.month == m:
        if d.weekday() < 5:
            count += 1
            if count == nth:
                return d
        d += timedelta(days=1)
    return date(y, m, 1)  # fallback


def _last_business_day(y: int, m: int) -> date:
    """Get last business day of a given month."""
    import calendar
    last_d = calendar.monthrange(y, m)[1]
    d = date(y, m, last_d)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def generate_recurring_events() -> list[dict]:
    today = date.today()
    window_end = today + timedelta(weeks=8)
    events = []

    def in_window(d: date) -> bool:
        return today <= d <= window_end

    def hk_event(ds: str, hkt: str, market: str, event: str, impact: str, cat: str, prev=None, consensus=None):
        return {"date": ds, "time_hkt": hkt, "market": market, "event": event,
                "impact": impact, "category": cat, "previous": prev, "consensus": consensus,
                "actual": None, "source": "hardcoded"}

    # ── FOMC ──
    for ds_et, ds_hkt in FOMC_MEETINGS_2026:
        d = date.fromisoformat(ds_hkt)  # HKT date (next day after ET decision)
        if in_window(d):
            events.append(hk_event(ds_hkt, "02:00", "US", "FOMC 利率決議", "High", "利率決策"))

    # ── Monthly US events ──
    for m_offset in range(4):
        y = today.year + (today.month + m_offset - 1) // 12
        m = ((today.month + m_offset - 1) % 12) + 1

        # NFP — 第一個星期五
        d = _nth_business_day(y, m, 1)
        while d.weekday() != 4:
            d += timedelta(days=1)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "20:30", "US", "非農就業 (NFP)", "High", "就業"))

        # ISM Manufacturing — 第一個工作日
        d = _nth_business_day(y, m, 1)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "22:00", "US", "ISM 製造業 PMI", "High", "經濟活動"))

        # ISM Services — 第三個工作日
        d = _nth_business_day(y, m, 3)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "22:00", "US", "ISM 非製造業 PMI", "High", "經濟活動"))

        # CPI ~13th
        cpi_day = min(13, 28)
        d = date(y, m, cpi_day)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "20:30", "US", "消費者物價指數 (CPI)", "High", "通膨"))

        # PPI ~14th
        ppi_day = min(14, 28)
        d = date(y, m, ppi_day)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "20:30", "US", "生產者物價指數 (PPI)", "High", "通膨"))

    # ── Weekly US ──
    for w_offset in range(8):
        d = today + timedelta(weeks=w_offset)
        while d.weekday() != 3:  # Thursday
            d += timedelta(days=1)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "20:30", "US", "首次申請失業救濟金", "Medium", "就業"))

    # ── China ──
    for ds in PBOC_LPR_2026:
        d = date.fromisoformat(ds)
        if in_window(d):
            events.append(hk_event(ds, "09:15", "China", "LPR 貸款市場報價利率", "High", "利率決策"))

    for m_offset in range(4):
        y = today.year + (today.month + m_offset - 1) // 12
        m = ((today.month + m_offset - 1) % 12) + 1

        # 中國官方 PMI — 本月最後一個工作日
        d = _last_business_day(y, m)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "09:30", "China", "官方製造業 PMI", "High", "經濟活動"))

        # 財新製造業 PMI — 第一個工作日
        d = _nth_business_day(y, m, 1)
        if in_window(d):
            events.append(hk_event(d.isoformat(), "09:45", "China", "財新製造業 PMI", "High", "經濟活動"))

        # 中國 CPI / PPI ~9th
        for label, day in [("CPI 消費者物價指數", 9), ("PPI 生產者出廠價格", 9)]:
            d = date(y, m, min(day, 28))
            if in_window(d):
                events.append(hk_event(d.isoformat(), "09:30", "China", f"中國 {label}", "High", "通膨"))

    return events


def fetch_finnhub_events() -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(weeks=2)).strftime("%Y-%m-%d")
    url = f"https://finnhub.io/api/v1/economic-calendar?token={FINNHUB_KEY}&from={today}&to={end}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[finnhub] {e}", file=sys.stderr)
        return []

    raw = data.get("economicCalendar", [])
    key_markets = {"US", "CN", "HK"}
    market_map = {"US": "US", "CN": "China", "HK": "HK"}
    events = []
    for e in raw:
        country = e.get("country", "")
        if country not in key_markets:
            continue
        dt = e.get("date", "")
        if not dt:
            continue  # skip events without dates
        events.append({
            "date": dt, "time_hkt": "--:--", "market": market_map.get(country, country),
            "event": e.get("event", ""),
            "impact": (e.get("impact") or "low").capitalize(),
            "category": "經濟活動",
            "previous": e.get("previous"), "consensus": e.get("estimate"),
            "actual": e.get("actual"), "source": "finnhub",
        })
    return events


def merge_events(finnhub_events: list, hardcoded: list) -> list:
    seen = set()
    merged = []
    for ev in hardcoded:
        key = f"{ev['date']}|{ev['event']}"
        if key not in seen:
            seen.add(key)
            merged.append(ev)
    for ev in finnhub_events:
        key = f"{ev['date']}|{ev['event']}"
        if key in seen:
            for ex in merged:
                if f"{ex['date']}|{ex['event']}" == key:
                    if ev.get("actual") and not ex.get("actual"):
                        ex["actual"] = ev["actual"]
                    if ev.get("consensus") and not ex.get("consensus"):
                        ex["consensus"] = ev["consensus"]
                    if ev.get("previous") and not ex.get("previous"):
                        ex["previous"] = ev["previous"]
                    break
        else:
            seen.add(key)
            merged.append(ev)
    merged.sort(key=lambda e: (e["date"], e.get("time_hkt", "99:99")))
    return merged


def save_calendar(events: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    now_hkt = now.strftime("%H:%M")
    today_events = [e for e in events if e["date"] == today_str]
    high_impact = [e for e in events if e["impact"] == "High"]

    # Flag events that have already passed today
    for ev in today_events:
        t = ev.get("time_hkt", "99:99")
        if t != "--:--" and t != "全天":
            ev["status"] = "已過" if t < now_hkt else "待發布"
        else:
            ev["status"] = "待發布"

    output = {
        "fetched_at": now.strftime("%Y-%m-%d %H:%M HKT"),
        "event_count": len(events),
        "today_count": len(today_events),
        "high_impact_count": len(high_impact),
        "today_events": today_events,
        "high_impact": high_impact,
        "events": events,
    }
    CALENDAR_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    return output


def main():
    now = datetime.now()
    print(f"=== Macro Calendar @ {now.strftime('%Y-%m-%d %H:%M')} HKT ===", flush=True)

    finnhub = fetch_finnhub_events()
    hardcoded = generate_recurring_events()
    events = merge_events(finnhub, hardcoded)
    result = save_calendar(events)

    today = result["today_events"]
    high = result["high_impact"]
    future_high = [e for e in high if e["date"] > now.strftime("%Y-%m-%d")]

    print(f"  Finnhub: {len(finnhub)} | Hardcoded: {len(hardcoded)} | Total: {len(events)} ({len(high)} high)", flush=True)
    print(f"  Today: {len(today)} events", flush=True)

    if today:
        print(f"\n  📅 今日事件:", flush=True)
        for ev in reversed(today):  # show upcoming first
            icon = "🔴" if ev["impact"] == "High" else "🟡"
            status = ev.get("status", "")
            status_icon = "✅" if status == "已過" else "⏳"
            print(f"    {icon} {status_icon} [{ev['time_hkt']}] {ev['market']} | {ev['event']} ({status})", flush=True)
    else:
        print(f"\n  ✅ 今日無重大經濟數據發布", flush=True)

    if future_high:
        print(f"\n  ⏰ 未來高影響事件:", flush=True)
        for ev in future_high[:5]:
            print(f"    🔴 {ev['date']} [{ev['time_hkt']}] {ev['market']} | {ev['event']}", flush=True)

    print(f"\n  Saved: {CALENDAR_PATH}", flush=True)
    print("\n---JSON_START---")
    print(json.dumps(result, ensure_ascii=False))
    print("---JSON_END---")


if __name__ == "__main__":
    main()
