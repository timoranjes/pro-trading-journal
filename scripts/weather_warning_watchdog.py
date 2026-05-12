#!/usr/bin/env python3
"""
HK Weather Warning Watchdog — polls HKO warnsum endpoint every 15 min.
Detects new, escalated, extended, or cancelled warnings.
Enhanced: also fetches rhrread + flw to enrich alerts with lightning,
heavy rainfall zones, general situation, and forecast context.

Only outputs when something changes.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HKT = timezone(timedelta(hours=8))
HKO_BASE = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
DATA_DIR = os.path.expanduser("~/.hermes/data")
STATE_FILE = os.path.join(DATA_DIR, "weather_warnings.json")

# Escalation hierarchy (higher = more severe) — mapped by warning code
ESCALATION_ORDER = {
    "WRAIN": {"AMBER": 1, "RED": 2, "BLACK": 3},
    "WTCS": {"T1": 1, "T3": 2, "T8NE": 3, "T8SE": 3, "T8NW": 3, "T8SW": 3, "T9": 4, "T10": 5},
    "WTS": {"ACTIVE": 1},
    "WHOT": {"ACTIVE": 1},
    "WCOLD": {"ACTIVE": 1},
    "WFIRE": {"LOW": 1, "MODERATE": 2, "HIGH": 3, "VERYHIGH": 4, "EXTREME": 5},
    "WMS": {"ACTIVE": 1},
    "WL": {"ACTIVE": 1},
    "WTC": {"LOW": 1, "MEDIUM": 2, "HIGH": 3},
    "WTSR": {"LOW": 1, "MEDIUM": 2, "HIGH": 3},
}

WARNING_EMOJI = {
    "WTS": "⛈️", "WRAIN": "🌧️", "WTCS": "🌀", "WHOT": "🔥",
    "WCOLD": "🥶", "WFIRE": "🔥", "WMS": "💨", "WL": "⛰️",
    "WTC": "🌊", "WTSR": "🌊",
}

KNOWN_WARNING_CODES = {"WTS", "WRAIN", "WTCS", "WHOT", "WCOLD", "WFIRE", "WMS", "WL", "WTC", "WTSR"}
EXPECTED_FIELDS = {"name", "code", "actionCode", "issueTime", "updateTime"}

# ─── Schema Validation ─────────────────────────────────────────

def validate_warnsum_schema(data):
    if not isinstance(data, dict):
        print(f"⚠️ SCHEMA_ERROR: HKO warnsum response is not a dict (type={type(data).__name__})")
        exit(1)
    if len(data) == 0:
        return True
    keys = set(data.keys())
    known_match = keys & KNOWN_WARNING_CODES
    if not known_match:
        sample = {k: type(data[k]).__name__ for k in list(keys)[:3]}
        print(f"⚠️ SCHEMA_ERROR: HKO warnsum unknown key structure: {sample}. Got: {list(keys)[:5]}")
        exit(1)
    sample = data[list(known_match)[0]]
    if not isinstance(sample, dict):
        print(f"⚠️ SCHEMA_ERROR: HKO warnsum item not a dict (type={type(sample).__name__})")
        exit(1)
    missing = EXPECTED_FIELDS - set(sample.keys())
    if missing:
        print(f"⚠️ SCHEMA_ERROR: HKO warnsum item missing fields: {missing}")
        exit(1)
    return True

def validate_rhrread_schema(data):
    if not isinstance(data, dict):
        print(f"⚠️ SCHEMA_ERROR: HKO rhrread is not a dict (type={type(data).__name__})")
        return False
    # Check at least one expected key exists
    expected_keys = {"temperature", "humidity", "rainfall", "icon"}
    if not expected_keys & set(data.keys()):
        print(f"⚠️ SCHEMA_ERROR: HKO rhrread missing expected keys. Got: {list(data.keys())[:5]}")
        return False
    return True

# ─── Data Fetching ──────────────────────────────────────────────

def fetch_hko(data_type, lang="en"):
    url = f"{HKO_BASE}?dataType={data_type}&lang={lang}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"⚠️ API_ERROR: Failed to fetch {data_type}: {e}")
        return None

def fetch_warnings():
    data = fetch_hko("warnsum")
    if data is None:
        return None
    validate_warnsum_schema(data)
    warnings = []
    for code, info in data.items():
        warnings.append({
            "code": code,
            "name": info.get("name", ""),
            "actionCode": info.get("actionCode", ""),
            "issueTime": info.get("issueTime", ""),
            "expireTime": info.get("expireTime", ""),
            "updateTime": info.get("updateTime", ""),
        })
    return warnings

def fetch_rich_context():
    """Fetch supporting data to enrich alerts. Returns dict or None."""
    rhr = fetch_hko("rhrread")
    if rhr and validate_rhrread_schema(rhr):
        pass  # proceed
    else:
        rhr = None
    flw = fetch_hko("flw")

    ctx = {"lightning": [], "rainfall": [], "situation": "", "forecast_desc": "",
           "outlook": "", "temp_hko": None, "humidity_hko": None, "wind_warning": ""}

    if rhr:
        # Lightning
        lightning = rhr.get("lightning", {})
        ctx["lightning"] = [l["place"] for l in lightning.get("data", []) if l.get("occur") == "true"]
        ctx["lightning_period"] = f"{short_time(lightning.get('startTime', ''))}–{short_time(lightning.get('endTime', ''))}" if lightning.get("startTime") else ""

        # Rainfall - heavy areas (>=5mm max)
        ctx["rainfall"] = []
        for r in rhr.get("rainfall", {}).get("data", []):
            if r.get("max", 0) >= 5:
                ctx["rainfall"].append({"place": r["place"], "max": r["max"], "min": r.get("min", 0)})
        ctx["rainfall_period"] = f"{short_time(rhr.get('rainfall', {}).get('startTime', ''))}–{short_time(rhr.get('rainfall', {}).get('endTime', ''))}" if rhr.get("rainfall", {}).get("startTime") else ""

        # Temperature
        for t in rhr.get("temperature", {}).get("data", []):
            if t.get("place") == "Hong Kong Observatory":
                ctx["temp_hko"] = t.get("value")
                break
        if ctx["temp_hko"] is None and rhr.get("temperature", {}).get("data"):
            ctx["temp_hko"] = rhr["temperature"]["data"][0].get("value")

        # Humidity
        for h in rhr.get("humidity", {}).get("data", []):
            if h.get("place") == "Hong Kong Observatory":
                ctx["humidity_hko"] = h.get("value")
                break

    if flw:
        ctx["situation"] = flw.get("generalSituation", "")
        ctx["forecast_desc"] = flw.get("forecastDesc", "")
        ctx["outlook"] = flw.get("outlook", "")
        # Wind warning from forecast desc
        desc = flw.get("forecastDesc", "").lower()
        if "force 8" in desc or "gale" in desc:
            ctx["wind_warning"] = "🌬️ Strong wind alert"
        elif "force 6" in desc or "force 7" in desc:
            ctx["wind_warning"] = "💨 Fresh to strong winds"
        elif "force 5" in desc:
            ctx["wind_warning"] = "💨 Moderate to fresh winds"

    return ctx

# ─── Change Detection ───────────────────────────────────────────

def get_warning_key(warning):
    return warning.get("code", "")

def get_severity(warning):
    code = warning.get("code", "")
    action = warning.get("actionCode", "").upper()
    if code in ESCALATION_ORDER:
        levels = ESCALATION_ORDER[code]
        for level_key, score in levels.items():
            if level_key in action:
                return score, level_key.title()
        if action in ("NEW", "UPDATE", "EXTEND"):
            return 1, "Active"
        if action == "CANCEL":
            return 0, "Cancelled"
    return 0, ""

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

def save_state(warnings, timestamp):
    os.makedirs(DATA_DIR, exist_ok=True)
    state = {"timestamp": timestamp, "warnings": []}
    for w in warnings:
        severity, level = get_severity(w)
        state["warnings"].append({
            "key": get_warning_key(w),
            "code": w.get("code", ""),
            "name": w.get("name", ""),
            "actionCode": w.get("actionCode", ""),
            "issueTime": w.get("issueTime", ""),
            "expireTime": w.get("expireTime", ""),
            "severity": severity,
            "level": level,
        })
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def detect_changes(warnings):
    now = datetime.now(HKT).strftime("%Y-%m-%d %H:%M")
    prev_state = load_state()
    if prev_state is None:
        save_state(warnings, now)
        return None

    prev_by_key = {w["key"]: w for w in prev_state.get("warnings", [])}
    curr_by_key = {}
    changes = []

    for w in warnings:
        key = get_warning_key(w)
        curr_by_key[key] = w
        severity, level = get_severity(w)

        if key not in prev_by_key:
            changes.append({
                "type": "NEW", "code": key, "emoji": WARNING_EMOJI.get(key, "⚠️"),
                "name": w.get("name", ""), "issueTime": w.get("issueTime", ""),
                "expireTime": w.get("expireTime", ""), "severity": severity, "level": level,
            })
        else:
            prev = prev_by_key[key]
            prev_action = prev.get("actionCode", "")
            curr_action = w.get("actionCode", "")
            if severity > prev.get("severity", 0):
                changes.append({
                    "type": "ESCALATED", "code": key, "emoji": WARNING_EMOJI.get(key, "⚠️"),
                    "name": w.get("name", ""), "from_level": prev.get("level", ""),
                    "to_level": level, "issueTime": w.get("issueTime", ""),
                    "expireTime": w.get("expireTime", ""), "severity": severity,
                })
            elif curr_action == "EXTEND" and prev_action != "EXTEND":
                changes.append({
                    "type": "EXTENDED", "code": key, "emoji": WARNING_EMOJI.get(key, "⚠️"),
                    "name": w.get("name", ""), "expireTime": w.get("expireTime", ""),
                    "issueTime": w.get("issueTime", ""), "updateTime": w.get("updateTime", ""),
                })

    for key, prev_w in prev_by_key.items():
        if key not in curr_by_key:
            changes.append({
                "type": "CANCELLED", "code": key, "emoji": WARNING_EMOJI.get(key, "⚠️"),
                "name": prev_w["name"], "severity": 0,
            })

    save_state(warnings, now)
    return changes if changes else None

# ─── Formatting ─────────────────────────────────────────────────

def short_time(t_str):
    if not t_str:
        return ""
    try:
        dt = datetime.fromisoformat(t_str)
        now = datetime.now(HKT)
        if dt.date() == now.date():
            return dt.strftime("%H:%M HKT")
        else:
            return dt.strftime("%m/%d %H:%M HKT")
    except (ValueError, TypeError):
        return t_str

def format_alert(changes, ctx):
    now = datetime.now(HKT)
    date_str = now.strftime("%Y-%m-%d %H:%M HKT")

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🌤 **HK Weather Alert**")
    lines.append(f"_{date_str}_")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # ── Warning blocks ──
    for change in changes:
        ctype = change["type"]
        emoji = change.get("emoji", "⚠️")
        name = change["name"]

        if ctype == "NEW":
            lines.append(f"{emoji} **{name}** · ⚠️ NEW")
            it = change.get("issueTime", "")
            et = change.get("expireTime", "")
            if it:
                lines.append(f"  Issued · {short_time(it)}")
            if et:
                lines.append(f"  Until  · {short_time(et)}")
            # Duration hint
            if it and et:
                try:
                    dts = datetime.fromisoformat(it)
                    dte = datetime.fromisoformat(et)
                    dur_hours = (dte - dts).total_seconds() / 3600
                    lines.append(f"  Duration · ~{dur_hours:.1f}h")
                except (ValueError, TypeError):
                    pass

        elif ctype == "EXTENDED":
            lines.append(f"{emoji} **{name}** · ⏳ EXTENDED")
            upt = change.get("updateTime", "")
            et = change.get("expireTime", "")
            if upt:
                lines.append(f"  Extended at · {short_time(upt)}")
            if et:
                lines.append(f"  Now until   · {short_time(et)}")
            # Update duration calc
            it = change.get("issueTime", "")
            if it and et:
                try:
                    dts = datetime.fromisoformat(it)
                    dte = datetime.fromisoformat(et)
                    dur_hours = (dte - dts).total_seconds() / 3600
                    lines.append(f"  Total duration · ~{dur_hours:.1f}h")
                except (ValueError, TypeError):
                    pass

        elif ctype == "ESCALATED":
            lines.append(f"{emoji} **{name}** · ⬆️ ESCALATED")
            lines.append(f"  {change.get('from_level', '')} → **{change.get('to_level', '')}**")
            et = change.get("expireTime", "")
            if et:
                lines.append(f"  Until · {short_time(et)}")

        elif ctype == "CANCELLED":
            lines.append(f"{emoji} **{name}** · ✅ CANCELLED")

        lines.append("")

    # ── Lightning section ──
    if ctx.get("lightning"):
        lines.append("⚡ **Lightning Detected**")
        lines.append(f"  {' · '.join(ctx['lightning'])}")
        if ctx.get("lightning_period"):
            lines.append(f"  _{ctx['lightning_period']}_")
        lines.append("")

    # ── Heavy rainfall section ──
    if ctx.get("rainfall"):
        lines.append("🌧️ **Heavy Rain (past hour)**")
        # Sort by max descending
        sorted_rain = sorted(ctx["rainfall"], key=lambda x: x["max"], reverse=True)
        for r in sorted_rain[:6]:
            if r["min"] and r["min"] != r["max"]:
                lines.append(f"  {r['place']} · {r['min']}–{r['max']}mm")
            else:
                lines.append(f"  {r['place']} · up to {r['max']}mm")
        if len(sorted_rain) > 6:
            lines.append(f"  _+{len(sorted_rain)-6} more areas_")
        if ctx.get("rainfall_period"):
            lines.append(f"  _{ctx['rainfall_period']}_")
        lines.append("")

    # ── Situation section ──
    situation = ctx.get("situation", "")
    if situation:
        lines.append("📋 **Situation**")
        # Keep it concise: first 2 sentences, no double-period artifacts
        import re
        sentences = [s.strip().rstrip(".") for s in re.split(r'(?<=[.!?])\s+', situation) if s.strip()]
        brief = ". ".join(sentences[:2])
        if brief:
            brief += "."
        if len(brief) > 250:
            brief = brief[:247] + "..."
        lines.append(f"  {brief}")
        lines.append("")

    # ── Forecast / Outlook ──
    forecast_desc = ctx.get("forecast_desc", "")
    if forecast_desc:
        lines.append("🔮 **Tonight's Forecast**")
        # Extract temp range and key info
        desc_clean = forecast_desc
        lines.append(f"  {desc_clean}")
        lines.append("")

    # ── Temperature snapshot ──
    temp = ctx.get("temp_hko")
    hum = ctx.get("humidity_hko")
    wind_warn = ctx.get("wind_warning", "")
    if temp is not None or hum is not None:
        parts = []
        if temp is not None:
            parts.append(f"🌡️ {temp}°C")
        if hum is not None:
            parts.append(f"💧 {hum}%")
        if wind_warn:
            parts.append(wind_warn)
        lines.append(" | ".join(parts))
        lines.append("")

    lines.append("───────────────────────────")
    lines.append("_Hong Kong Observatory_ · next check in 15 min")

    return "\n".join(lines)


# ─── Quiet Hours ────────────────────────────────────────────────

QUIET_HOURS_START = 23  # 23:00 HKT
QUIET_HOURS_END = 7     # 07:00 HKT

SILENT_CHANGE_TYPES = {"EXTENDED"}  # skip these during quiet hours
URGENT_CHANGE_TYPES = {"NEW", "ESCALATED", "CANCELLED"}  # always deliver


def is_quiet_hours():
    """Check if current HKT time falls within quiet hours (23:00–07:00)."""
    now = datetime.now(HKT)
    hour = now.hour
    if QUIET_HOURS_START <= hour or hour < QUIET_HOURS_END:
        return True
    return False


def filter_quiet_hours(changes):
    """Filter out non-urgent changes during quiet hours.
    Returns (filtered_changes, was_filtered, had_urgent)."""
    if not is_quiet_hours():
        return changes, False, True

    filtered = [c for c in changes if c["type"] in URGENT_CHANGE_TYPES]
    was_filtered = len(filtered) < len(changes)
    had_urgent = len(filtered) > 0
    return filtered, was_filtered, had_urgent


# ─── Main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    warnings = fetch_warnings()
    if warnings is None:
        print("⚠️ Failed to fetch warnings — exiting silently.")
        exit(0)

    changes = detect_changes(warnings)
    if not changes:
        print("NO_CHANGE")
        exit(0)

    # If no active warnings remain (all cleared), skip delivery entirely
    if not warnings:
        print("NO_CHANGE")
        exit(0)

    # Quiet hours filter
    changes, was_filtered, had_urgent = filter_quiet_hours(changes)

    if not had_urgent:
        # All changes were EXTEND during quiet hours — silent
        if was_filtered:
            # Still update state (already done by detect_changes)
            pass
        print("NO_CHANGE")
        exit(0)

    # Fetch rich context to enrich the alert
    ctx = fetch_rich_context()

    if was_filtered:
        output = format_alert(changes, ctx)
        # Prepend quiet hours note
        output = (
            "🌙 **Quiet Hours** · Only urgent alerts shown\n"
            "   EXTEND notifications suppressed until 07:00 HKT\n\n"
        ) + output
        print(output)
    else:
        print(format_alert(changes, ctx))
