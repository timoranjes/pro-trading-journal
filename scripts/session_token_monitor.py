#!/usr/bin/env python3
"""Session token budget monitor — AUTO-ARCHIVE ENFORCER.

Behavior:
    - Reads sessions.json for token counts
    - CRITICAL (>200K tokens AND >2h old): auto-archive (move .jsonl → archive/, suspend session)
    - WARNING (>150K AND >24h old): same treatment
    - Empty stdout = silent (no issues detected)

Cron: */30 6-22 * * 1-5  deliver: local  no_agent: true
"""
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

SESSIONS_FILE = Path.home() / ".hermes" / "sessions" / "sessions.json"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
ARCHIVE_DIR = SESSIONS_DIR / "archive"
CRITICAL_THRESHOLD = 150_000
WARN_THRESHOLD = 100_000
MIN_AGE_HOURS = 1
CROSS_DAY_AGE_HOURS = 24


HKT_OFFSET = timedelta(hours=8)


def parse_age_hours(updated_str: str, now: datetime) -> float:
    """Parse the updated_at timestamp and return age in hours.

    The gateway stores naive timestamps in local time (HKT, +08:00).
    We convert them to UTC before comparison.
    """
    if not updated_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(updated_str)
        if dt.tzinfo is None:
            # Assume HKT (local time) and subtract offset to get UTC
            dt = dt.replace(tzinfo=timezone.utc) - HKT_OFFSET
        return (now - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0.0


def main():
    if not SESSIONS_FILE.exists():
        return

    with open(SESSIONS_FILE) as f:
        data = json.load(f)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    modified = False

    for key, session in dict(data).items():
        tokens = session.get("last_prompt_tokens", 0)
        if session.get("suspended", False):
            continue

        age_hours = parse_age_hours(session.get("updated_at", ""), now)

        should_archive = False
        if tokens >= CRITICAL_THRESHOLD and age_hours >= MIN_AGE_HOURS:
            should_archive = True
        elif tokens >= WARN_THRESHOLD and age_hours >= CROSS_DAY_AGE_HOURS:
            should_archive = True

        if not should_archive:
            continue

        # Archive the session's JSONL history file(s)
        sid = session.get("session_id", "")
        for jsonl_file in SESSIONS_DIR.glob(f"{sid}*.jsonl"):
            try:
                dest = ARCHIVE_DIR / jsonl_file.name
                shutil.move(str(jsonl_file), str(dest))
            except Exception:
                pass  # non-critical; session state still gets suspended

        # Mark session as suspended in sessions.json
        data[key]["suspended"] = True
        data[key]["last_prompt_tokens"] = 0
        data[key]["message_count"] = 0
        modified = True

    if modified:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # Always print summary for cron logging (verifies execution)
    active = sum(1 for s in data.values() if not s.get("suspended", False))
    suspended = sum(1 for s in data.values() if s.get("suspended", False))
    total_tokens = sum(s.get("last_prompt_tokens", 0) for s in data.values())
    over_150 = sum(1 for s in data.values() if s.get("last_prompt_tokens", 0) >= 150_000 and not s.get("suspended", False))
    over_100 = sum(1 for s in data.values() if 100_000 <= s.get("last_prompt_tokens", 0) < 150_000 and not s.get("suspended", False))
    print(f"[{now.strftime('%Y-%m-%d %H:%M UTC')}] Sessions: {len(data)} total, {active} active, {suspended} suspended | Tokens: {total_tokens:,} | >150K: {over_150} | >100K: {over_100} | Archived: {modified}")


if __name__ == "__main__":
    main()
