#!/usr/bin/env python3
"""
WhatsApp Church Service Tracker

Monitors WhatsApp church group messages for service time announcements,
syncs to iCloud "Church" calendar, and sends Discord notifications.

Features:
- Parses WhatsApp messages for service times (Saturday Pursuit, Sunday District, etc.)
- Creates/updates events in iCloud "Church" calendar
- Sends Discord notifications to #church-life before services
- Tracks state to avoid duplicate processing

Usage:
    python3 whatsapp_church_tracker.py [--sync-only] [--notify-only]
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env for API keys (cron jobs don't inherit shell environment)
_env_path = Path.home() / ".hermes" / ".env"
if _env_path.exists():
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                os.environ.setdefault(_key, _val)

# HKT timezone
HKT = timezone(timedelta(hours=8))

# Configuration
STATE_DIR = Path.home() / ".hermes" / "cron" / "output" / "church" / "whatsapp-tracker"
STATE_FILE = STATE_DIR / "state.json"
ROSTER_STATE_FILE = STATE_DIR / "roster_state.json"
PROCESSED_FILE = STATE_DIR / "processed_messages.json"
DISCORD_WEBHOOKS_FILE = Path.home() / ".hermes" / "data" / "webhooks.json"
CHURCH_LIFE_CHANNEL_ID = "1499014349119815742"  # #church-life

# Roster patterns to detect service assignments
ROSTER_PATTERNS = {
    "offering_box": {
        "header": r"(奉献箱|開箱|奉獻箱).*服事",
        "line": r"(\d{1,2})[日號].*?([^\n~#]+)",
        "group_name": "沙田區奉献箱服事",
        "group_jid": "85293491592-1474614965@g.us",
        "clean_pattern": r"[,，\s]+$"  # Remove trailing commas and whitespace
    },
    "bulletin_translation": {
        "header": r"(translation service roster|週訊翻譯|翻譯服事)",
        "line": r"(\d{1,2}/\d{1,2})\s+([^\n]+)",
        "group_name": "召會週訊翻譯",
        "group_jid": "85262019415-1550915413@g.us",
        "clean_pattern": None
    },
    "pursuit_speaker": {
        "header": r"追求人位安排",
        "line": r"(週[一二三四五六日])[:：]\s*([^\n]+)",
        "group_name": "週六追求群",
        "group_jid": "85261731466-1546654850@g.us",
        "clean_pattern": None
    },
    "blend_meeting": {
        "header": r"(roster|服事安排|配搭|Updated)",
        "line": r"(\d{1,2}/\d{1,2})\s+([^\n]+)",
        "group_name": "Blend with ST district 1",
        "group_jid": "85262820917-1548403817@g.us",
        "clean_pattern": r"[\*\s]+$"  # Remove trailing asterisks and whitespace
    },
    "piano_request": {
        "header": r"(司琴|piano|司他|guitar)",
        "line": r"(\d{1,2}/\d{1,2}).*?(司琴|彈琴|play piano)",
        "group_name": "司琴司他服事",
        "group_jid": "85264313858-1474602833@g.us",
        "clean_pattern": None
    }
}

# Service patterns to detect in WhatsApp messages
# Each pattern requires:
#   1. Day-of-week keyword
#   2. Service type keyword
#   3. Time expression
#   4. (Optional but recommended) Date context: 本周/下週/5/10 etc.
#
# "require_date_context" means the message MUST contain a date reference
# (本周X, 下週X, M/D, X月X號) to prevent false positives from casual chat.
SERVICE_PATTERNS = {
    "saturday_pursuit": {
        "pattern": r"(星期六|周六|Saturday)\s*(追求|Pursuit)\s*[聚會meeting]?\s*[改到在at]?\s*(\d{1,2}[:：]\d{2}|[上午下午][\d一二三四五六七八九十]+)",
        "default_time": "10:00",
        "duration_minutes": 90,
        "title": "Saturday Pursuit (追求聚會)",
        "notify_minutes_before": 30,
        "require_date_context": False  # Pursuit group messages are usually official
    },
    "sunday_district": {
        "pattern": r"(星期日|周日|Sunday)\s*([區districtD][\u4e00-\u9fff]*|District)\s*[聚會meeting安排]\s*[在at]?\s*(\d{1,2}[:：]\d{2}|[上午下午][\d一二三四五六七八九十]+)",
        "default_time": "10:00",
        "duration_minutes": 120,
        "title": "Sunday District Meeting (區聚會)",
        "notify_minutes_before": 30,
        "require_date_context": True  # Must mention 本周/下週/5/10 to create event
    },
    "sunday_evening": {
        "pattern": r"(星期日|周日|Sunday)\s*[的之]?(晚上|evening)\s*[聚會meeting]?\s*[在at]?\s*(\d{1,2}[:：]\d{2})",
        "default_time": "19:30",
        "duration_minutes": 90,
        "title": "Sunday Evening Meeting (晚上聚會)",
        "notify_minutes_before": 30,
        "require_date_context": True
    },
    "wednesday_prayer": {
        "pattern": r"(星期三|周三|Wednesday)\s*(禱告|prayer|Prayer)\s*[聚會]?\s*[在at]?\s*(\d{1,2}[:：]\d{2})",
        "default_time": "19:30",
        "duration_minutes": 60,
        "title": "Wednesday Prayer Meeting (禱告聚會)",
        "notify_minutes_before": 30,
        "require_date_context": True
    },
    "friday_prophecy": {
        "pattern": r"(星期五|周五|Friday)\s*(申言|prophecy|Prophecy)\s*[聚會]?\s*[在at]?\s*(\d{1,2}[:：]\d{2})",
        "default_time": "19:30",
        "duration_minutes": 90,
        "title": "Friday Prophecy Meeting (申言聚會)",
        "notify_minutes_before": 30,
        "require_date_context": True
    }
}

# Chinese time expressions mapping
TIME_MAP = {
    "上午": "AM",
    "下午": "PM",
    "早上": "AM",
    "晚上": "PM",
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"
}


def ensure_dirs():
    """Create necessary directories."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state():
    """Load tracker state."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_sync": None,
        "events_created": [],
        "notifications_sent": []
    }


def save_state(state):
    """Save tracker state."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_processed_messages():
    """Load list of already processed message IDs."""
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"message_ids": []}


def save_processed_message(msg_id):
    """Mark a message as processed."""
    data = load_processed_messages()
    if msg_id not in data["message_ids"]:
        data["message_ids"].append(msg_id)
        # Keep only last 1000 message IDs
        data["message_ids"] = data["message_ids"][-1000:]
        with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def load_roster_state():
    """Load stored roster state."""
    if ROSTER_STATE_FILE.exists():
        with open(ROSTER_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"rosters": {}}


def save_roster_state(roster_state):
    """Save roster state."""
    with open(ROSTER_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(roster_state, f, indent=2, ensure_ascii=False)


def normalize_date_key(date_str):
    """Normalize date strings to D/M format for consistent roster key comparison.
    
    Handles M/D, D/M, M/DD, D/M formats.
    e.g., '5/24' → '24/5', '3/5' → '3/5', '10/5' → '10/5'
    """
    match = re.match(r'(\d{1,2})/(\d{1,2})', date_str)
    if not match:
        return date_str
    
    a, b = int(match.group(1)), int(match.group(2))
    
    if a > 12:
        return date_str  # Already D/M format
    elif b > 12:
        return f"{b}/{a}"  # M/D format, convert to D/M
    else:
        return date_str  # Both <= 12, keep as-is (context-dependent)


def detect_roster_changes(new_assignments):
    """Compare new roster assignments against stored state. Returns list of changes."""
    roster_state = load_roster_state()
    changes = []
    
    # Deduplicate new assignments - keep only the latest per roster_type + group_jid + date
    # Since WhatsApp returns messages in reverse chronological order (newest first),
    # the first occurrence is the latest
    deduped = {}
    for assignment in new_assignments:
        roster_key = f"{assignment['roster_type']}_{assignment['group_jid']}"
        date_key = normalize_date_key(assignment['date_or_day'])
        entry_key = f"{roster_key}_{date_key}"
        
        # Keep only the first occurrence (latest message)
        if entry_key not in deduped:
            deduped[entry_key] = assignment
    
    # Compare against stored state
    for entry_key, assignment in deduped.items():
        stored_value = roster_state.get("rosters", {}).get(entry_key, {}).get("assigned_to", "")
        new_value = assignment['assigned_to']
        
        # Normalize values for comparison (remove extra whitespace, punctuation, asterisks, tildes)
        stored_normalized = re.sub(r'[\s~，,*]+', '', stored_value).lower()
        new_normalized = re.sub(r'[\s~，,*]+', '', new_value).lower()
        
        # Also remove Chinese characters that might differ (弟兄/姊妹)
        stored_normalized = re.sub(r'[弟兄姊妹]', '', stored_normalized)
        new_normalized = re.sub(r'[弟兄姊妹]', '', new_normalized)
        
        if stored_value and stored_normalized != new_normalized:
            changes.append({
                "type": "changed",
                "group": assignment['group'],
                "roster_type": assignment['roster_type'],
                "date_or_day": assignment['date_or_day'],
                "old_value": stored_value,
                "new_value": new_value
            })
        elif not stored_value:
            changes.append({
                "type": "new",
                "group": assignment['group'],
                "roster_type": assignment['roster_type'],
                "date_or_day": assignment['date_or_day'],
                "assigned_to": new_value
            })
    
    # Update stored state with deduped assignments (latest values)
    for entry_key, assignment in deduped.items():
        if "rosters" not in roster_state:
            roster_state["rosters"] = {}
        
        roster_state["rosters"][entry_key] = {
            "assigned_to": assignment['assigned_to'],
            "group": assignment['group'],
            "date_or_day": assignment['date_or_day'],
            "last_updated": datetime.now(HKT).isoformat()
        }
    
    save_roster_state(roster_state)
    return changes


def is_message_processed(msg_id):
    """Check if message was already processed."""
    data = load_processed_messages()
    return msg_id in data["message_ids"]


def parse_time(time_str):
    """Parse Chinese/English time string to 24h format."""
    if not time_str:
        return None
    
    time_str = time_str.strip()
    
    # Handle HH:MM or HH：MM format
    time_str = time_str.replace("：", ":")
    
    # Check for AM/PM indicators
    am_pm = None
    for cn, en in TIME_MAP.items():
        if cn in time_str:
            am_pm = en
            time_str = time_str.replace(cn, "")
            break
    
    # Extract numbers
    match = re.search(r"(\d{1,2})[:：]?(\d{2})?", time_str)
    if not match:
        return None
    
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    
    # Apply AM/PM
    if am_pm == "PM" and hour < 12:
        hour += 12
    elif am_pm == "AM" and hour == 12:
        hour = 0
    
    return f"{hour:02d}:{minute:02d}"


def parse_whatsapp_message(text):
    """Parse WhatsApp message for service times and dates.

    Only creates events when:
    1. Pattern matches the service type keyword + time
    2. If require_date_context=True, message must also contain date context
       (本周/下週/下週/5/10/5月10號) to prevent false positives from casual chat.
    """
    services = []

    # Date extraction patterns — find "本周六", "下週日", "5/10", etc.
    date_info = _extract_date_from_text(text)
    has_date_context = date_info["type"] != "this" or _has_explicit_date_ref(text)

    for service_key, config in SERVICE_PATTERNS.items():
        match = re.search(config["pattern"], text, re.IGNORECASE)
        if match:
            # Skip if this service requires date context but message lacks it
            if config.get("require_date_context") and not has_date_context:
                print(f"   ⏭️ Skipping {service_key}: pattern matched but no explicit date reference")
                continue

            # Extract time from match
            time_groups = [g for g in match.groups() if g and ":" in g]
            time_str = time_groups[0] if time_groups else config["default_time"]

            parsed_time = parse_time(time_str) or config["default_time"]

            services.append({
                "type": service_key,
                "title": config["title"],
                "time": parsed_time,
                "duration_minutes": config["duration_minutes"],
                "notify_minutes_before": config["notify_minutes_before"],
                "date_hint": date_info  # "this", "next", or specific date
            })

    return services


def _has_explicit_date_ref(text):
    """Check if text contains an explicit date reference.
    Returns True for: 本周X, 下週X, 今週X, 5/10, 5月10號, May 10, next week.
    Returns False for: messages that only say 'Saturday' or 'Sunday' without
    specifying WHICH Saturday/Sunday.
    """
    if re.search(r"(本周|今週|这週|本週|下週|下周|下禮拜|next\s*week)", text, re.IGNORECASE):
        return True
    if re.search(r"(\d{1,2})[/月](\d{1,2})[號日]?", text):
        return True
    if re.search(r"(May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December)\s*\d{1,2}", text, re.IGNORECASE):
        return True
    return False


def _extract_date_from_text(text):
    """Extract date context from message text.
    Returns: {"type": "this"|"next"|"specific", "date": datetime|None}
    """
    now = datetime.now(HKT)

    # "本周X" / "今週X" / "这週X" → this week
    if re.search(r"(本周|今週|这週|本週)", text):
        return {"type": "this", "date": None}

    # "下週X" / "下周" / "下禮拜" → next week
    if re.search(r"(下週|下周|下禮拜|next\s*week)", text, re.IGNORECASE):
        return {"type": "next", "date": None}

    # Specific dates: "5/10", "5月10號", "May 10"
    month_day = re.search(r"(\d{1,2})[/月](\d{1,2})[號日]?", text)
    if month_day:
        month = int(month_day.group(1))
        day = int(month_day.group(2))
        year = now.year
        # If the date already passed this year, assume next year
        try:
            target = datetime(year, month, day)
            if target < now:
                target = datetime(year + 1, month, day)
            return {"type": "specific", "date": target}
        except ValueError:
            pass

    return {"type": "this", "date": None}


def detect_roster_assignments(text, group_jid):
    """Detect service roster/assignment messages and extract assignments."""
    assignments = []
    
    for roster_key, config in ROSTER_PATTERNS.items():
        # Only process messages from the correct group
        if group_jid != config["group_jid"]:
            continue
            
        # Check if this message matches the roster header pattern
        if re.search(config["header"], text, re.IGNORECASE):
            # Extract individual assignment lines
            for line in text.split('\n'):
                line = line.strip()
                if not line or len(line) < 5:
                    continue
                
                line_match = re.search(config["line"], line)
                if line_match:
                    date_or_day = line_match.group(1)
                    assigned_to = line_match.group(2).strip()
                    
                    # Clean up the assigned_to string using the clean_pattern
                    if config.get("clean_pattern"):
                        assigned_to = re.sub(config["clean_pattern"], "", assigned_to)
                    
                    assignments.append({
                        "roster_type": roster_key,
                        "group": config["group_name"],
                        "group_jid": group_jid,
                        "date_or_day": date_or_day,
                        "assigned_to": assigned_to,
                        "raw_line": line
                    })
    
    return assignments


# Keywords that indicate a roster-related message (for LLM fallback trigger)
ROSTER_KEYWORDS = [
    "司琴", "司他", "吉他", "guitar", "piano",
    "服事", "安排", "roster", "updated",
    "彈琴", "負責", "換", "調"
]

# LLM config for roster extraction
LLM_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = "https://coding.dashscope.aliyuncs.com/v1"
LLM_MODEL = "kimi-k2.5"  # Good at Chinese text extraction


def _has_roster_keywords(text):
    """Check if message contains roster-related keywords."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in ROSTER_KEYWORDS)


def _llm_extract_assignments(text, group_jid):
    """Use LLM to extract roster assignments from unstructured text.
    
    Returns list of assignment dicts matching detect_roster_assignments format.
    """
    if not LLM_API_KEY:
        print("   ⚠️ DASHSCOPE_API_KEY not set — skipping LLM roster fallback")
        return []
    
    # Map group_jid to roster_type and group_name
    jid_to_roster = {
        "85264313858-1474602833@g.us": ("piano_request", "司琴司他服事"),
        "85293491592-1474614965@g.us": ("offering_box", "沙田區奉献箱服事"),
        "85262019415-1550915413@g.us": ("bulletin_translation", "召會週訊翻譯"),
        "85261731466-1546654850@g.us": ("pursuit_speaker", "週六追求群"),
        "85262820917-1548403817@g.us": ("blend_meeting", "Blend with ST district 1"),
    }
    
    roster_type, group_name = jid_to_roster.get(group_jid, ("unknown", "Unknown"))
    
    prompt = f"""你是教會服事排班助手。請從以下 WhatsApp 訊息中提取所有服事安排。

訊息內容：
{text}

請以 JSON 格式返回，格式如下：
[
  {{"date": "3/5", "assigned_to": "嘉儀, 齊恩"}},
  {{"date": "10/5", "assigned_to": "Orange"}}
]

規則：
- date 格式保持原文 (如 3/5, 5/24, 週一)
- assigned_to 是用 comma 分隔的人名
- 如果沒有找到任何安排，返回空陣列 []
- 只返回 JSON，不要有其他文字"""

    url = f"{LLM_BASE_URL}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0,
    }
    
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", url,
             "-H", f"Authorization: Bearer {LLM_API_KEY}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            print(f"   ⚠️ LLM roster extraction failed: {result.stderr[:200]}")
            return []
        
        resp = json.loads(result.stdout)
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if not json_match:
            print(f"   ⚠️ LLM returned no JSON array: {content[:200]}")
            return []
        
        assignments_data = json.loads(json_match.group())
        
        assignments = []
        for item in assignments_data:
            date = item.get("date", "")
            assigned = item.get("assigned_to", "")
            if date and assigned:
                assignments.append({
                    "roster_type": roster_type,
                    "group": group_name,
                    "group_jid": group_jid,
                    "date_or_day": date,
                    "assigned_to": assigned.strip(),
                    "raw_line": f"{date} → {assigned}",
                    "source": "llm"
                })
        
        if assignments:
            print(f"   🤖 LLM extracted {len(assignments)} assignment(s)")
        
        return assignments
    
    except (json.JSONDecodeError, KeyError, subprocess.TimeoutExpired) as e:
        print(f"   ⚠️ LLM roster extraction error: {e}")
        return []


def get_next_occurrence(day_name, time_str, date_hint=None):
    """Get next occurrence of a day+time combination, respecting date hints."""
    day_map = {
        "saturday": 5, "sunday": 6, "wednesday": 2, "friday": 4
    }

    # Map service type to day
    day_num = None
    for key, num in day_map.items():
        if key in day_name.lower():
            day_num = num
            break

    if day_num is None:
        return None

    now = datetime.now(HKT)
    target = now.replace(hour=int(time_str.split(":")[0]),
                         minute=int(time_str.split(":")[1]),
                         second=0, microsecond=0)

    # If date_hint specifies a concrete date, use that
    if date_hint and date_hint.get("type") == "specific" and date_hint.get("date"):
        specific = date_hint["date"]
        target = target.replace(year=specific.year, month=specific.month, day=specific.day)
        return target

    # Calculate days to target weekday
    days_ahead = day_num - now.weekday()

    # Respect "this week" vs "next week" hint
    if date_hint and date_hint.get("type") == "next":
        # Force next week even if it's the same weekday
        if days_ahead <= 0:
            days_ahead += 7
        else:
            days_ahead += 7  # Go one week further
    elif date_hint and date_hint.get("type") == "this":
        # This week — if days_ahead < 0, wrap to next week is correct
        pass
    else:
        # No hint: default behavior (this week, or next week if passed)
        if days_ahead < 0:
            days_ahead += 7

    target = target + timedelta(days=days_ahead)

    # If today and time already passed, move to next week
    if days_ahead == 0 and target < now:
        target = target + timedelta(days=7)

    return target


def create_ical_event(service, start_dt):
    """Create iCal event string."""
    end_dt = start_dt + timedelta(minutes=service["duration_minutes"])
    
    # Format datetime for iCal (YYYYMMDDTHHMMSS)
    start_str = start_dt.strftime("%Y%m%dT%H%M%S")
    end_str = end_dt.strftime("%Y%m%dT%H%M%S")
    
    # Unique ID
    uid = f"{service['type']}-{start_dt.strftime('%Y%m%d')}@church-tracker"
    
    # Create event
    event = f"""BEGIN:VEVENT
UID:{uid}
DTSTAMP:{datetime.now(HKT).strftime('%Y%m%dT%H%M%S')}
DTSTART:{start_str}
DTEND:{end_str}
SUMMARY:{service['title']}
DESCRIPTION:Service time from WhatsApp church group
CATEGORIES:Church Service
BEGIN:VALARM
TRIGGER:-PT{service['notify_minutes_before']}M
ACTION:DISPLAY
DESCRIPTION:Reminder: {service['title']} starts in {service['notify_minutes_before']} minutes
END:VALARM
END:VEVENT"""
    
    return event


def check_event_exists(cal_name, title, start_date):
    """Check if an event with the same title already exists on the given date."""
    target_str = start_date.strftime("%Y-%m-%d")
    applescript = f'''
    tell application "Calendar"
        set cal to calendar "{cal_name}"
        set matchingEvents to (every event of cal whose summary is "{title}")
        set found to false
        repeat with evt in matchingEvents
            set evtStart to start date of evt
            set evtStr to (year of evtStart as text) & "-" & (month of evtStart as integer) & "-" & (day of evtStart as text)
            if evtStr is "{target_str}" then
                set found to true
                exit repeat
            end if
        end repeat
        return found
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False  # On error, proceed with creation (safer to potentially duplicate than miss)


def sync_to_icloud_calendar(services):
    """Sync services to iCloud Church calendar using AppleScript."""
    if not services:
        print("ℹ️ No services to sync")
        return True
    
    for service in services:
        next_occurrence = get_next_occurrence(service["type"], service["time"], service.get("date_hint"))
        if not next_occurrence:
            print(f"⚠️ Could not determine next occurrence for {service['title']}")
            continue
        
        # Deduplication: skip if event already exists for this date
        if check_event_exists("Church", service["title"], next_occurrence):
            print(f"⏭️ Event already exists: {service['title']} at {next_occurrence.strftime('%Y-%m-%d %H:%M')}")
            continue
        
        end_time = next_occurrence + timedelta(minutes=service["duration_minutes"])
        
        # AppleScript to create calendar event
        # Use current date as base and modify components
        applescript = f'''
        tell application "Calendar"
            set cal to calendar "Church"
            
            -- Start with current date and modify
            set eventStart to current date
            set year of eventStart to {next_occurrence.year}
            set month of eventStart to {next_occurrence.month}
            set day of eventStart to {next_occurrence.day}
            set hours of eventStart to {next_occurrence.hour}
            set minutes of eventStart to {next_occurrence.minute}
            set seconds of eventStart to 0
            
            set eventEnd to current date
            set year of eventEnd to {end_time.year}
            set month of eventEnd to {end_time.month}
            set day of eventEnd to {end_time.day}
            set hours of eventEnd to {end_time.hour}
            set minutes of eventEnd to {end_time.minute}
            set seconds of eventEnd to 0
            
            set eventTitle to "{service['title']}"
            
            make new event at end of events of cal with properties {{summary:eventTitle, start date:eventStart, end date:eventEnd}}
        end tell
        '''
        
        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                print(f"✅ Created calendar event: {service['title']} at {next_occurrence.strftime('%Y-%m-%d %H:%M')}")
            else:
                print(f"❌ Failed to create event: {result.stderr}")
        except subprocess.TimeoutExpired:
            print(f"⚠️ Timeout creating event for {service['title']}")
        except Exception as e:
            print(f"❌ Error: {e}")
    
    return True


def send_discord_notification(service, event_time):
    """Send Discord notification for upcoming service."""
    notify_time = event_time - timedelta(minutes=service["notify_minutes_before"])
    now = datetime.now(HKT)
    
    # Only send if notification time is within 5 minutes
    time_diff = (notify_time - now).total_seconds()
    if time_diff > 300 or time_diff < -60:
        return False
    
    # Load Discord webhook
    webhook_url = None
    if DISCORD_WEBHOOKS_FILE.exists():
        with open(DISCORD_WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            webhooks = json.load(f)
            webhook_url = webhooks.get(CHURCH_LIFE_CHANNEL_ID)
    
    if not webhook_url:
        print(f"⚠️ No Discord webhook configured for channel {CHURCH_LIFE_CHANNEL_ID}")
        return False
    
    # Format message
    message = f"""🔔 **Reminder: {service['title']}**

🕐 **Time:** {event_time.strftime('%H:%M')} HKT
📅 **Date:** {event_time.strftime('%Y-%m-%d (%A)')}

⏰ Starting in {service['notify_minutes_before']} minutes!"""

    payload = {
        "content": message,
        "username": "Church Service Tracker"
    }
    
    try:
        result = subprocess.run(
            ["curl", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", json.dumps(payload), webhook_url],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            print(f"✅ Discord notification sent for {service['title']}")
            return True
        else:
            print(f"❌ Failed to send Discord notification: {result.stderr}")
    except Exception as e:
        print(f"❌ Error sending Discord notification: {e}")
    
    return False


def send_roster_change_notification(changes):
    """Send Discord notification for roster changes."""
    if not changes:
        return
    
    webhook_url = None
    if DISCORD_WEBHOOKS_FILE.exists():
        with open(DISCORD_WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            webhooks = json.load(f)
            webhook_url = webhooks.get(CHURCH_LIFE_CHANNEL_ID)
    
    if not webhook_url:
        print(f"⚠️ No Discord webhook configured for channel {CHURCH_LIFE_CHANNEL_ID}")
        return False
    
    # Build message
    lines = ["📋 **Roster Update Detected**\n"]
    
    for change in changes:
        if change["type"] == "changed":
            lines.append(f"🔄 **{change['group']}** | {change['date_or_day']}")
            lines.append(f"   {change['old_value']} → **{change['new_value']}**")
        else:
            lines.append(f"🆕 **{change['group']}** | {change['date_or_day']}")
            lines.append(f"   → **{change['assigned_to']}**")
    
    # Highlight Orange's assignments
    orange_changes = [c for c in changes if "orange" in str(c.get('new_value', c.get('assigned_to', ''))).lower()]
    if orange_changes:
        lines.append(f"\n⭐ **Your assignments ({len(orange_changes)}):**")
        for c in orange_changes:
            val = c.get('new_value', c.get('assigned_to', ''))
            lines.append(f"   ⭐ {c['group']} | {c['date_or_day']} | {val}")
    
    message = "\n".join(lines)
    
    payload = {
        "content": message,
        "username": "Church Service Tracker"
    }
    
    try:
        result = subprocess.run(
            ["curl", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", json.dumps(payload), webhook_url],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            print(f"✅ Discord roster change notification sent ({len(changes)} changes)")
        else:
            print(f"❌ Failed to send roster notification: {result.stderr}")
    except Exception as e:
        print(f"❌ Error sending roster notification: {e}")


def check_pending_notifications():
    """Check for any pending notifications that should be sent."""
    state = load_state()
    now = datetime.now(HKT)
    
    for event in state.get("events_created", []):
        event_time = datetime.strptime(event["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=HKT)
        notify_time = event_time - timedelta(minutes=event.get("notify_minutes_before", 30))
        
        # Check if we should send notification now (within 2 minute window)
        time_diff = (notify_time - now).total_seconds()
        if -120 <= time_diff <= 120:
            # Check if already sent
            notification_key = f"{event['type']}-{event['time']}"
            if notification_key not in state.get("notifications_sent", []):
                send_discord_notification(event, event_time)
                state.setdefault("notifications_sent", []).append(notification_key)
                save_state(state)


def read_whatsapp_messages():
    """Read recent messages from WhatsApp church groups using wacli.
    
    Returns list of (message_id, text, group_jid, media_type) tuples.
    """
    messages = []
    image_messages = []  # Separate list for image messages
    
    # Try to use wacli to read recent messages
    # Note: This requires wacli to be configured and authenticated
    try:
        # Church service announcement groups
        church_groups = [
            "85260414258-1599974430@g.us",  # 沙田 6-8 區 弟兄們
            "120363020112280421@g.us",  # 沙田青少年服事🌱
            "85264313858-1474602833@g.us",  # 🎹🎸沙田區🌱司琴司他服事🎶
            "85293491592-1474614965@g.us",  # 沙田區奉献箱服事😄
            "85262019415-1550915413@g.us",  # 召會週訊翻譯📖
            "85261731466-1546654850@g.us",  # 週六追求群👑
            "85262820917-1548403817@g.us",  # Blend with ST district 1
        ]
        
        for group_jid in church_groups:
            # Read last 50 messages from group
            result = subprocess.run(
                ["wacli", "messages", "list", "--chat", group_jid, "--json", "--limit", "50"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                try:
                    chat_data = json.loads(result.stdout)
                    messages_list = chat_data.get("data", {}).get("messages", [])
                    for msg in messages_list:
                        msg_id = msg.get("MsgID")
                        text = msg.get("Text", "")
                        media_type = msg.get("MediaType", "")
                        
                        # Track image messages separately
                        if media_type == "image" and not is_message_processed(msg_id):
                            image_messages.append((msg_id, group_jid, msg.get("Timestamp", "")))
                            save_processed_message(msg_id)
                        
                        # Track text messages
                        if text and not is_message_processed(msg_id):
                            messages.append((msg_id, text, group_jid, media_type))
                            save_processed_message(msg_id)
                except json.JSONDecodeError:
                    print(f"⚠️ Failed to parse wacli output for {group_jid}")
            else:
                print(f"⚠️ wacli failed for {group_jid}: {result.stderr}")
    
    except FileNotFoundError:
        print("⚠️ wacli not found - WhatsApp integration unavailable")
    except Exception as e:
        print(f"⚠️ Error reading WhatsApp: {e}")
    
    return messages, image_messages


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="WhatsApp Church Service Tracker")
    parser.add_argument("--sync-only", action="store_true", help="Only sync to calendar")
    parser.add_argument("--notify-only", action="store_true", help="Only check pending notifications")
    args = parser.parse_args()
    
    ensure_dirs()
    
    if args.notify_only:
        print("🔔 Checking pending notifications...")
        check_pending_notifications()
        return
    
    print("📱 WhatsApp Church Service Tracker")
    print("=" * 40)
    
    # Read actual WhatsApp messages from church groups
    print("\n📖 Reading WhatsApp messages...")
    whatsapp_messages, image_messages = read_whatsapp_messages()
    
    if whatsapp_messages:
        print(f"   Found {len(whatsapp_messages)} new message(s)")
    else:
        print("   No new text messages")
    
    if image_messages:
        print(f"   Found {len(image_messages)} new image message(s)")
    
    if not whatsapp_messages and not image_messages:
        print("   No new messages (or wacli not configured)")
        # Do NOT fall back to sample data — sample messages create fake calendar events
        print("ℹ️ Skipping service detection (wacli unavailable)")
        return
    
    # Parse all messages for service times and roster assignments
    all_services = []
    all_roster_assignments = []

    # Groups authorized for service time announcements
    service_announcement_groups = {
        "85260414258-1599974430@g.us",  # 沙田 6-8 區 弟兄們
        "120363020112280421@g.us",  # 沙田青少年服事🌱
        "85262820917-1548403817@g.us",  # Blend with ST district 1
    }

    for msg_id, text, group_jid, media_type in whatsapp_messages:
        # Only create service events from authorized announcement groups
        if group_jid in service_announcement_groups:
            services = parse_whatsapp_message(text)
            if services:
                print(f"\n📅 Detected from message: {text[:80]}")
                all_services.extend(services)
        else:
            print(f"   ⏭️ Skipping service detection from {group_jid} (not an announcement group)")

        # Detect roster assignments (these use group-specific patterns, so always check)
        assignments = detect_roster_assignments(text, group_jid)
        
        # LLM fallback: if regex found nothing but message has roster keywords
        if not assignments and _has_roster_keywords(text):
            print(f"   🤖 Regex found 0 assignments, trying LLM fallback for message: {text[:80]}")
            assignments = _llm_extract_assignments(text, group_jid)
        
        if assignments:
            all_roster_assignments.extend(assignments)
    
    # Process image messages (for piano/guitar roster)
    if image_messages:
        print("\n🖼️ Processing image messages...")
        for msg_id, group_jid, timestamp in image_messages:
            if group_jid == "85264313858-1474602833@g.us":  # 司琴司他服事
                print(f"   Downloading image {msg_id}...")
                # Download image
                output_path = f"/tmp/church_rosters/piano_{msg_id}.jpg"
                subprocess.run([
                    "wacli", "media", "download",
                    "--chat", group_jid,
                    "--id", msg_id,
                    "--output", output_path
                ], capture_output=True, text=True, timeout=30)
                
                # Analyze image with vision
                try:
                    from hermes_tools import vision_analyze
                    result = vision_analyze(
                        image_url=output_path,
                        question="Extract the complete piano/guitar service roster table. List all dates (3/5, 10/5, 17/5, 24/5, 31/5) and assigned musicians for each location. Format as: DATE: musician1, musician2, musician3"
                    )
                    
                    if result.get('success') and result.get('analysis'):
                        # Parse the analysis to extract roster data
                        analysis = result['analysis']
                        # Extract date assignments from the analysis
                        date_pattern = r'(\d{1,2}/\d{1,2})[:：]\s*([^\n]+)'
                        for match in re.finditer(date_pattern, analysis):
                            date = match.group(1)
                            musicians = match.group(2).strip()
                            
                            all_roster_assignments.append({
                                "roster_type": "piano_request",
                                "group": "司琴司他服事",
                                "group_jid": group_jid,
                                "date_or_day": date,
                                "assigned_to": musicians,
                                "raw_line": f"{date}: {musicians}",
                                "source": "image"
                            })
                            print(f"   🎹 Extracted: {date} → {musicians}")
                except Exception as e:
                    print(f"   ⚠️ Failed to analyze image: {e}")
    
    # Display roster assignments (especially for Orange)
    if all_roster_assignments:
        print(f"\n📋 Found {len(all_roster_assignments)} roster assignment(s):")
        orange_assignments = [a for a in all_roster_assignments if "orange" in a["assigned_to"].lower()]
        
        for assignment in all_roster_assignments:
            print(f"   • [{assignment['group']}] {assignment['date_or_day']} → {assignment['assigned_to']}")
        
        if orange_assignments:
            print(f"\n🔔 **Orange's assignments ({len(orange_assignments)}):**")
            for a in orange_assignments:
                print(f"   ⭐ {a['group']} | {a['date_or_day']} | {a['assigned_to']}")
        
        # Detect and notify roster changes
        changes = detect_roster_changes(all_roster_assignments)
        if changes:
            print(f"\n🔄 Detected {len(changes)} roster change(s):")
            for change in changes:
                if change["type"] == "changed":
                    print(f"   🔄 {change['group']} | {change['date_or_day']}: {change['old_value']} → {change['new_value']}")
                else:
                    print(f"   🆕 {change['group']} | {change['date_or_day']}: → {change['assigned_to']}")
            
            # Send Discord notification for roster changes
            send_roster_change_notification(changes)
    
    if all_services:
        print(f"\n📅 Found {len(all_services)} service(s):")
        for svc in all_services:
            next_occ = get_next_occurrence(svc["type"], svc["time"], svc.get("date_hint"))
            if next_occ:
                print(f"   • {svc['title']}: {next_occ.strftime('%Y-%m-%d %H:%M')} HKT")
        
        # Sync to iCloud
        sync_to_icloud_calendar(all_services)
        
        # Save state
        state = load_state()
        state["last_sync"] = datetime.now(HKT).isoformat()
        state["events_created"] = [
            {
                "type": s["type"],
                "title": s["title"],
                "time": get_next_occurrence(s["type"], s["time"], s.get("date_hint")).strftime("%Y-%m-%d %H:%M:%S"),
                "notify_minutes_before": s["notify_minutes_before"]
            }
            for s in all_services
        ]
        save_state(state)
    else:
        print("\nℹ️ No service times detected in messages")
    
    # Check for pending notifications
    check_pending_notifications()
    
    print("\n✅ Tracker complete")


if __name__ == "__main__":
    main()
