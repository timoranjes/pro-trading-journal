#!/usr/bin/env python3
"""
research_persistence_gate.py — Ensure sessions that conduct research save their findings.

Trigger: on_session_end (fires at end of each run_conversation() turn)
Payload (stdin JSON):
    {"hook_event_name": "on_session_end", "session_id": "...", "extra": {...}}

Logic:
1. Query the session DB for assistant messages with tool_calls.
2. Parse tool_calls JSON to extract function names.
3. Detect research tools vs persistence tools.
4. If research WITHOUT persistence → emit compliance warning to stderr.

This gate enforces AGENTS.md Layer 1:
  "NEVER end a session that involved research without saving the findings."
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

# --- External research tools (fetch new data) — MUST persist findings ---
RESEARCH_TOOLS = frozenset([
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_back",
    "browser_console",
    "browser_get_images",
    "browser_vision",
])

# --- Local lookup tools (read existing data) — no persistence required ---
LOOKUP_TOOLS = frozenset([
    "session_search",  # Recalls past sessions, doesn't generate new findings
])

# --- Persistence tool names ---
PERSISTENCE_TOOLS = frozenset([
    "memory",
    "skill_manage",
    "hindsight_retain",
    "write_file",
    "patch",
])

SWARMVAULT_PREFIX = "mcp_swarmvault_"

# Pattern to extract tool name from tool-role message content
# e.g. "Tool 'search_files' does not exist..."
TOOL_NAME_PATTERN = re.compile(r"^Tool '(\w+)'")


def get_hermes_db_path() -> Path:
    """Find the Hermes session database."""
    home = Path.home()
    candidates = [
        home / ".hermes" / "state.db",
        home / ".hermes" / "hermes-agent" / "hermes.db",
        home / ".hermes" / "sessions.db",
        home / ".clawd" / "hermes.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def classify_tool(tool_name: str) -> tuple:
    """Return (is_research, is_persistence) for a tool name."""
    is_research = tool_name in RESEARCH_TOOLS
    is_persistence = (
        tool_name in PERSISTENCE_TOOLS
        or tool_name.startswith(SWARMVAULT_PREFIX)
    )
    return is_research, is_persistence


def extract_tools_from_tool_calls(tool_calls_json: str) -> list:
    """Parse tool_calls JSON and return list of function names."""
    names = []
    try:
        calls = json.loads(tool_calls_json)
        if not isinstance(calls, list):
            calls = [calls]
        for call in calls:
            if isinstance(call, dict):
                fn = call.get("function", {}).get("name", "")
                if fn:
                    names.append(fn)
            elif isinstance(call, str):
                names.append(call)
    except (json.JSONDecodeError, TypeError):
        pass
    return names


def check_session(session_id: str) -> dict:
    """Check a session for research and persistence tool usage."""
    db_path = get_hermes_db_path()
    if not db_path.exists():
        return {
            "research_tools": [],
            "persistence_tools": [],
            "error": f"DB not found at {db_path}",
        }

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        research_used = set()
        persistence_used = set()

        # 1. Check assistant messages with tool_calls (primary source)
        cursor.execute(
            """
            SELECT tool_calls FROM messages
            WHERE session_id = ? AND role = 'assistant'
              AND tool_calls IS NOT NULL AND tool_calls != 'null'
              AND tool_calls != ''
            """,
            (session_id,),
        )
        for row in cursor.fetchall():
            tool_calls_json = row[0]
            if not tool_calls_json:
                continue
            for fn_name in extract_tools_from_tool_calls(tool_calls_json):
                is_res, is_pers = classify_tool(fn_name)
                if is_res:
                    research_used.add(fn_name)
                if is_pers:
                    persistence_used.add(fn_name)

        # 2. Fallback: check tool-role messages for "Tool 'xxx'" pattern
        cursor.execute(
            """
            SELECT content FROM messages
            WHERE session_id = ? AND role = 'tool'
              AND content LIKE 'Tool ''%'''
            """,
            (session_id,),
        )
        for row in cursor.fetchall():
            content = row[0] or ""
            m = TOOL_NAME_PATTERN.match(content)
            if m:
                fn_name = m.group(1)
                is_res, is_pers = classify_tool(fn_name)
                if is_res:
                    research_used.add(fn_name)
                if is_pers:
                    persistence_used.add(fn_name)

        conn.close()

        return {
            "research_tools": sorted(research_used),
            "persistence_tools": sorted(persistence_used),
            "error": None,
        }

    except sqlite3.Error as e:
        return {
            "research_tools": [],
            "persistence_tools": [],
            "error": f"DB query failed: {e}",
        }


def main():
    try:
        payload_str = sys.stdin.read()
        if not payload_str.strip():
            return

        payload = json.loads(payload_str)
    except (json.JSONDecodeError, Exception) as e:
        sys.stderr.write(f"[research_persistence_gate] Payload error: {e}\n")
        print("{}")
        return

    session_id = payload.get("session_id", "")
    if not session_id:
        print("{}")
        return

    result = check_session(session_id)

    if result["error"]:
        sys.stderr.write(f"[research_persistence_gate] {result['error']}\n")
        print("{}")
        return

    research = result["research_tools"]
    persistence = result["persistence_tools"]

    if research and not persistence:
        tools_str = ", ".join(research)
        msg = (
            f"[research_persistence_gate] ⚠️ COMPLIANCE VIOLATION: "
            f"Session {session_id} used research tools ({tools_str}) "
            f"but did NOT persist findings via memory/skill_manage/"
            f"hindsight_retain/swarmvault. "
            f"Findings will be lost — next session re-researches from scratch."
        )
        sys.stderr.write(msg + "\n")
        sys.stderr.write(
            "[research_persistence_gate] Suggested: use memory (facts), "
            "skill_manage (procedures), or mcp_swarmvault_* (synthesis) "
            "before session ends.\n"
        )
    elif research and persistence:
        sys.stderr.write(
            f"[research_persistence_gate] ✓ Session {session_id}: "
            f"research ({', '.join(research)}) persisted via "
            f"{', '.join(persistence)}\n"
        )

    # Always return empty JSON — observer hook
    print("{}")


if __name__ == "__main__":
    main()
