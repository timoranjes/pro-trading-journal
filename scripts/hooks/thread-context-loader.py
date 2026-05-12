#!/usr/bin/env python3
"""Rich thread context loader — injects prior conversation context when a
Discord thread needs continuity.

Registered as a ``pre_llm_call`` shell hook. Fires on every turn but only
injects context when appropriate:

1. **Inactivity gap within same session** (>4 hours) → recover context
2. **New session after system event** (gateway crash, OOM, timeout) → recover
3. **New session after user /reset or /new** → NO recovery (clean slate)

The distinction between #2 and #3 uses the gap between the prior session's
last activity and the current time. If the gap is small (<5 minutes), it's
likely a user-initiated reset. If larger, it's likely a system event.

Payload from run_agent.py includes: thread_id, chat_id, platform, session_id,
is_first_turn, model, sender_id, user_message, conversation_history.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
THREAD_MAP = HERMES_HOME / "data" / "thread-session-map.json"
GAP_STATE = HERMES_HOME / "data" / "thread-context-state.json"
SESSIONS_DIR = HERMES_HOME / "sessions"
SESSIONS_INDEX = SESSIONS_DIR / "sessions.json"
SWARMVAULT_WIKI = HERMES_HOME.parent / "wiki-ministry-words" / "wiki" if (HERMES_HOME.parent / "wiki-ministry-words").exists() else None

# Constants
MAX_CONTENT_CHARS = 500  # Truncate individual messages
MAX_MESSAGES_PER_SESSION = 30  # Read up to 30 key messages per prior session
MAX_SESSIONS_TO_SUMMARIZE = 1  # Only load the single most recent prior session
INACTIVITY_THRESHOLD_SEC = 4 * 3600  # 4 hours for same-session gap
SESSION_RESET_THRESHOLD_SEC = 5 * 60  # 5 minutes to distinguish /reset from system event

# Patterns that indicate user wants a clean slate
RESET_PATTERNS = [
    "/reset", "/new", "start over", "start fresh", "clear context",
    "new session", "fresh start", "forget everything", "ignore previous",
    "let's start over", "scratch that",
]


def load_json(path: Path) -> dict:
    """Load a JSON file, returning empty dict on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_json(path: Path, data: dict):
    """Save a JSON file."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def extract_session_metadata(session_id: str) -> dict:
    """Extract metadata from sessions.json index."""
    if not SESSIONS_INDEX.exists():
        return {}
    try:
        with open(SESSIONS_INDEX) as f:
            index = json.load(f)
        for key, entry in index.items():
            if entry.get("session_id") == session_id:
                return entry
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def get_session_last_activity_time(session_id: str) -> Optional[float]:
    """Get the timestamp of the last activity in a session (from JSONL)."""
    session_file = SESSIONS_DIR / f"{session_id}.jsonl"
    if not session_file.exists():
        return None

    # First try sessions.json for the updated_at timestamp
    metadata = extract_session_metadata(session_id)
    updated = metadata.get("updated_at")
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass

    # Fall back: scan the JSONL file for the last message timestamp
    last_time = None
    try:
        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = obj.get("timestamp") or obj.get("created_at")
                    if ts:
                        if isinstance(ts, (int, float)):
                            last_time = ts
                        elif isinstance(ts, str):
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                last_time = dt.timestamp()
                            except (ValueError, TypeError):
                                pass
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return last_time


def classify_message(role: str, content: str) -> Optional[dict]:
    """Extract structured info from a message. Returns None if skip-worthy."""
    if not content or len(content) < 10:
        return None

    if content.startswith("[Sent") or content.startswith("[CONTEXT COMPACTION"):
        return None

    info = {"role": role, "summary": "", "is_key": False}

    if role == "user":
        if content.startswith("[Replying to:"):
            if "\n\n" in content:
                content = content.split("\n\n")[-1]
        info["summary"] = content.strip()
        info["is_key"] = True

    elif role == "assistant":
        if any(marker in content for marker in [
            "Done.", "Created", "Updated", "Pushed", "Deployed",
            "Fixed", "Committed", "Found the issue", "Root cause",
            "Here's what I", "What changed", "Key decisions",
            "Unresolved", "Open items", "Next step", "Pending",
            "I've created", "I've updated", "I've modified",
        ]):
            info["is_key"] = True
        if len(content) > 1000:
            info["is_key"] = True

        if len(content) > MAX_CONTENT_CHARS:
            info["summary"] = content[:MAX_CONTENT_CHARS] + "..."
        else:
            info["summary"] = content.strip()

    return info


def extract_session_summary(session_id: str) -> Optional[str]:
    """Extract a rich summary from a JSONL session file."""
    session_file = SESSIONS_DIR / f"{session_id}.jsonl"
    if not session_file.exists():
        return None

    try:
        messages = []
        compaction_summary = None

        with open(session_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    role = obj.get("role")
                    content = obj.get("content", "")

                    if role == "assistant" and "[CONTEXT COMPACTION" in content:
                        compaction_summary = content
                        continue

                    classified = classify_message(role, content)
                    if classified:
                        messages.append(classified)
                except json.JSONDecodeError:
                    continue

        if not messages:
            return None

        parts = []

        if compaction_summary:
            summary_text = compaction_summary.split("\n\n", 1)[-1] if "\n\n" in compaction_summary else compaction_summary
            if len(summary_text) > 100:
                parts.append("📋 **Prior Context (compacted):**\n" + summary_text[:600])

        key_messages = [m for m in messages if m["is_key"]]

        if len(key_messages) > MAX_MESSAGES_PER_SESSION:
            user_msgs = [m for m in key_messages if m["role"] == "user"][:3]
            assistant_msgs = [m for m in key_messages if m["role"] == "assistant"][-6:]
            selected = user_msgs + assistant_msgs
        else:
            selected = key_messages

        if selected:
            exchange_parts = []
            current_user = None
            for msg in selected:
                if msg["role"] == "user":
                    if current_user:
                        exchange_parts.append(current_user)
                    current_user = f"**User:** {msg['summary'][:300]}"
                else:
                    if current_user:
                        exchange_parts.append(current_user)
                        current_user = None
                    exchange_parts.append(f"**Agent:** {msg['summary'][:300]}")
            if current_user:
                exchange_parts.append(current_user)

            parts.append("\n\n".join(exchange_parts))

        deliverables = []
        full_text = " ".join(m["summary"] for m in messages[-10:])

        commit_match = re.search(r"commit [`']?([a-f0-9]+)", full_text)
        if commit_match:
            deliverables.append(f"Commit: `{commit_match.group(1)}`")

        if "Vercel" in full_text and ("deploy" in full_text.lower() or "building" in full_text.lower()):
            deliverables.append("Vercel deployment triggered")

        if "pushed" in full_text.lower():
            deliverables.append("Git push completed")

        if deliverables:
            parts.append("📦 **Deliverables:**\n" + "\n".join(f"- {d}" for d in deliverables))

        return "\n\n".join(parts)

    except Exception:
        return None


def check_swarmvault(thread_topic: str) -> Optional[str]:
    """Check SwarmVault for related pages/tasks (best effort, no network)."""
    if not SWARMVAULT_WIKI or not SWARMVAULT_WIKI.exists():
        return None

    try:
        outputs_dir = SWARMVAULT_WIKI / "outputs"
        if outputs_dir.exists():
            recent_files = sorted(outputs_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)[:2]
            if recent_files:
                parts = []
                for rf in recent_files:
                    content = rf.read_text()[:400]
                    parts.append(f"**{rf.name}:**\n{content}")
                return "\n\n".join(parts)
    except Exception:
        pass
    return None


def build_thread_key(thread_sessions: list) -> str:
    """Build a topic key from the thread's session history."""
    if not thread_sessions:
        return "unknown"
    latest = thread_sessions[-1]
    metadata = extract_session_metadata(latest)
    if metadata.get("display_name"):
        return metadata["display_name"]
    return "unknown"


def is_reset_intent(user_message: str) -> bool:
    """Check if the user's message indicates intent for a clean slate."""
    if not user_message:
        return False
    msg_lower = user_message.lower().strip()
    return any(pattern in msg_lower for pattern in RESET_PATTERNS)


def build_context(session_id: str, thread_map: dict, key: str, sessions_to_load: list, trigger_reason: str):
    """Build the context injection payload."""
    prior_sessions = [s for s in thread_map.get(key, []) if s != session_id and not s.startswith("test_") and (SESSIONS_DIR / f"{s}.jsonl").exists()]

    context_parts = []
    context_parts.append(
        f"🔄 **Context Recovery Triggered:** {trigger_reason}\n\n"
        f"📌 **Thread Context Recovery** — Found {len(prior_sessions)} prior session(s) for this thread. "
        f"Current session: `{session_id}`\n"
        f"Loading context from the most recent prior session to help you pick up where you left off:\n"
    )

    for prior_sid in sessions_to_load:
        metadata = extract_session_metadata(prior_sid)
        created = metadata.get("created_at", "unknown")[:16].replace("T", " ")
        title = metadata.get("display_name", "unknown")

        header = f"--- Previous Session (ID: `{prior_sid}`, started: {created}) ---"
        if title and title != "unknown":
            header += f"\n📂 **Thread:** {title}"
        context_parts.append(header)

        summary = extract_session_summary(prior_sid)
        if summary:
            context_parts.append(summary)
        else:
            context_parts.append(f"*(No detailed summary available for session {prior_sid})*")

        context_parts.append("")

    topic_key = build_thread_key(sessions_to_load)
    sv_context = check_swarmvault(topic_key)
    if sv_context:
        context_parts.append("🗃️ **SwarmVault Related Context:**\n" + sv_context)

    context_parts.append(
        "---\n"
        "⚡ **Instructions:** Use the context above to understand what was being worked on, "
        "what decisions were made, and what remains pending. Do NOT re-solve already-completed work. "
        "If the user says 'continue' or 'go ahead', resume from the last pending item."
    )

    context = "\n\n".join(context_parts)
    return {"context": context}


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, IOError):
        return

    is_first_turn = payload.get("is_first_turn", False)
    thread_id = (payload.get("thread_id") or "").strip()
    chat_id = (payload.get("chat_id") or "").strip()
    session_id = (payload.get("session_id") or "").strip()
    platform = (payload.get("platform") or "").strip()
    user_message = payload.get("user_message", "")

    # Only care about Discord threads
    if not thread_id or platform != "discord":
        return

    now = time.time()

    # --- Step 1: Record current session mapping ---
    thread_map = load_json(THREAD_MAP)
    key = f"{chat_id}:{thread_id}" if chat_id else thread_id

    if key not in thread_map:
        thread_map[key] = []
    if session_id not in thread_map[key]:
        thread_map[key].append(session_id)
    save_json(THREAD_MAP, thread_map)

    # --- Step 2: Load per-thread gap state ---
    gap_state = load_json(GAP_STATE)
    thread_state = gap_state.get(key, {})
    prev_session_id = thread_state.get("session_id")
    prev_msg_time = thread_state.get("last_msg_time")

    # Check if we've already recovered context for this session
    if thread_state.get("context_recovered_for") == session_id:
        # Update timestamp and return
        thread_state["session_id"] = session_id
        thread_state["last_msg_time"] = now
        gap_state[key] = thread_state
        save_json(GAP_STATE, gap_state)
        return

    # --- Step 3: Determine if context recovery should trigger ---
    should_recover = False
    trigger_reason = ""

    if is_first_turn:
        # New session — distinguish user-initiated reset from system event

        # Signal A: User message explicitly indicates clean slate intent
        if is_reset_intent(user_message):
            # User wants fresh start — don't recover
            thread_state["session_id"] = session_id
            thread_state["last_msg_time"] = now
            thread_state["context_recovered_for"] = session_id  # Mark as handled
            gap_state[key] = thread_state
            save_json(GAP_STATE, gap_state)
            return

        # Signal B: Find the most recent prior session and check timing
        prior_sessions = []
        for s in thread_map.get(key, []):
            if s == session_id or s.startswith("test_"):
                continue
            if not (SESSIONS_DIR / f"{s}.jsonl").exists():
                continue
            prior_sessions.append(s)

        if not prior_sessions:
            # No prior sessions — first session for this thread
            thread_state["session_id"] = session_id
            thread_state["last_msg_time"] = now
            thread_state["context_recovered_for"] = session_id
            gap_state[key] = thread_state
            save_json(GAP_STATE, gap_state)
            return

        most_recent_prior = prior_sessions[-1]
        prior_last_activity = get_session_last_activity_time(most_recent_prior)

        if prior_last_activity:
            gap = now - prior_last_activity
            if gap < SESSION_RESET_THRESHOLD_SEC:
                # Small gap — likely user typed /reset and immediately started new session
                # Don't recover context (respect user intent for clean slate)
                thread_state["session_id"] = session_id
                thread_state["last_msg_time"] = now
                thread_state["context_recovered_for"] = session_id
                gap_state[key] = thread_state
                save_json(GAP_STATE, gap_state)
                return
            else:
                # Significant gap — likely system event (gateway crash, OOM, timeout)
                should_recover = True
                gap_min = gap / 60
                trigger_reason = f"system restart (session gap: {gap_min:.0f} min)"
        else:
            # Can't determine prior session timing — default to recovering
            should_recover = True
            trigger_reason = "system restart (prior session timing unknown)"

    elif prev_session_id and prev_session_id != session_id:
        # Session changed mid-conversation (gateway restart during active chat)
        should_recover = True
        trigger_reason = "session changed mid-conversation (system event)"

    elif prev_msg_time:
        # Same session — check for inactivity gap
        gap = now - prev_msg_time
        if gap >= INACTIVITY_THRESHOLD_SEC:
            should_recover = True
            gap_hr = gap / 3600
            trigger_reason = f"inactivity gap of {gap_hr:.1f} hours"

    # --- Step 4: Update state ---
    thread_state["session_id"] = session_id
    thread_state["last_msg_time"] = now

    if not should_recover:
        gap_state[key] = thread_state
        save_json(GAP_STATE, gap_state)
        return

    # --- Step 5: Build and return context ---
    # Re-filter prior sessions (exclude current, test, non-existent)
    prior_sessions = []
    for s in thread_map.get(key, []):
        if s == session_id or s.startswith("test_"):
            continue
        if not (SESSIONS_DIR / f"{s}.jsonl").exists():
            continue
        prior_sessions.append(s)

    if not prior_sessions:
        thread_state["context_recovered_for"] = session_id
        gap_state[key] = thread_state
        save_json(GAP_STATE, gap_state)
        return

    sessions_to_load = prior_sessions[-MAX_SESSIONS_TO_SUMMARIZE:]
    thread_state["context_recovered_for"] = session_id
    gap_state[key] = thread_state
    save_json(GAP_STATE, gap_state)

    result = build_context(session_id, thread_map, key, sessions_to_load, trigger_reason)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
