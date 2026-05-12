#!/usr/bin/env python3
"""
Enhanced Pattern Analyzer for Hermes Agent
Filters stop words and focuses on meaningful topic extraction.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
SESSION_DIR = HERMES_HOME / "sessions"
DATA_DIR = HERMES_HOME / "data"
PATTERNS_FILE = DATA_DIR / "session_patterns.json"
SUGGESTIONS_FILE = DATA_DIR / "proactive_suggestions.json"

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'this', 'that', 'from', 'have', 'been', 'are',
    'was', 'were', 'been', 'being', 'have', 'has', 'had', 'having', 'do', 'does',
    'did', 'doing', 'will', 'would', 'could', 'should', 'may', 'might', 'must',
    'shall', 'can', 'need', 'dare', 'ought', 'used', 'to', 'of', 'in', 'on', 'at',
    'by', 'about', 'against', 'between', 'into', 'through', 'during', 'before',
    'after', 'above', 'below', 'out', 'off', 'over', 'under', 'again', 'further',
    'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all', 'both',
    'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not',
    'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just', 'because', 'as',
    'until', 'while', 'if', 'or', 'but', 'what', 'which', 'who', 'whom', 'whose',
    'your', 'you', 'they', 'them', 'their', 'its', 'our', 'my', 'his', 'her',
    'we', 'he', 'she', 'it', 'me', 'him', 'is', 'am', 'be', 'an', 'a', 'i',
    'silent', 'output', 'report', 'check', 'run', 'get', 'make', 'use', 'please',
    'ok', 'yes', 'no', 'go', 'ahead', 'working', 'done', 'error', 'issue'
}

def extract_meaningful_topics(text):
    """Extract meaningful topics from text, filtering stop words."""
    # Find domain-specific keywords and phrases
    patterns = [
        r'(?:market|portfolio|stock|trading|price|alert|news|earnings)',
        r'(?:cron|job|schedule|monitor|watch|alert)',
        r'(?:skill|plugin|config|setting|setup)',
        r'(?:memory|hindsight|session|context|prompt)',
        r'(?:code|debug|test|build|deploy|script)',
        r'(?:github|repo|commit|branch|pr|merge)',
        r'(?:discord|telegram|webhook|message|channel)',
        r'(?:pdf|report|html|presentation|slide)',
        r'(?:model|llm|api|token|context|reasoning)',
    ]
    
    topics = []
    text_lower = text.lower()
    
    for pattern in patterns:
        matches = re.findall(pattern, text_lower)
        topics.extend(matches)
    
    # Also extract proper nouns and technical terms
    technical_words = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', text)
    topics.extend([w.lower() for w in technical_words])
    
    return topics

def analyze_sessions():
    """Analyze recent session logs for meaningful patterns."""
    patterns = {
        "frequent_topics": {},
        "tool_usage": {},
        "error_patterns": {},
        "time_patterns": {},
        "skill_usage": {}
    }
    
    cutoff = datetime.now() - timedelta(days=7)
    session_files = []
    
    if SESSION_DIR.exists():
        for f in SESSION_DIR.rglob("*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime >= cutoff:
                    session_files.append(f)
            except:
                continue
    
    for session_file in session_files:
        try:
            with open(session_file, 'r') as f:
                session = json.load(f)
            
            if 'messages' in session and session['messages']:
                first_msg = session['messages'][0]
                if first_msg.get('role') == 'user':
                    content = first_msg.get('content', '')
                    topics = extract_meaningful_topics(content)
                    for topic in topics:
                        patterns["frequent_topics"][topic] = patterns["frequent_topics"].get(topic, 0) + 1
            
            for msg in session.get('messages', []):
                if msg.get('role') == 'assistant':
                    for tool_call in msg.get('tool_calls', []):
                        tool_name = tool_call.get('function', {}).get('name', 'unknown')
                        patterns["tool_usage"][tool_name] = patterns["tool_usage"].get(tool_name, 0) + 1
                        if 'error' in str(tool_call):
                            patterns["error_patterns"][tool_name] = patterns["error_patterns"].get(tool_name, 0) + 1
                
                content = str(msg.get('content', ''))
                skill_matches = re.findall(r'skill_view\(name=[\'"]([^\'"]+)[\'"]\)', content)
                for skill in skill_matches:
                    patterns["skill_usage"][skill] = patterns["skill_usage"].get(skill, 0) + 1
                    
        except Exception as e:
            continue
    
    for key in patterns:
        patterns[key] = dict(sorted(patterns[key].items(), key=lambda x: x[1], reverse=True)[:20])
    
    return patterns

def generate_suggestions(patterns):
    """Generate actionable suggestions with implementation tasks."""
    suggestions = []
    
    for topic, count in patterns["frequent_topics"].items():
        if count >= 3:
            suggestions.append({
                "type": "skill_suggestion",
                "trigger": f"Frequent '{topic}' requests ({count} times)",
                "action": f"Create/install skill for '{topic}' workflow automation",
                "priority": "high" if count >= 5 else "medium",
                "implementable": True,
                "implementation_hint": f"Search existing skills for '{topic}' workflow, enhance or create new skill"
            })
    
    for tool, count in patterns["tool_usage"].items():
        if count >= 10 and tool not in ['web_search', 'terminal', 'read_file']:
            suggestions.append({
                "type": "optimization",
                "trigger": f"High '{tool}' usage ({count} times)",
                "action": f"Batch '{tool}' operations or create wrapper",
                "priority": "medium",
                "implementable": True,
                "implementation_hint": f"Create wrapper script or optimize {tool} call patterns in AGENTS.md"
            })
    
    for tool, count in patterns["error_patterns"].items():
        if count >= 3:
            suggestions.append({
                "type": "error_prevention",
                "trigger": f"Recurring '{tool}' errors ({count} times)",
                "action": f"Create troubleshooting skill for '{tool}'",
                "priority": "high",
                "implementable": True,
                "implementation_hint": f"Analyze {tool} error logs, add pitfall section to relevant skill or create new troubleshooting guide"
            })
    
    # Skill compliance check — if skill_view() rate is low, flag it
    total_tool_calls = sum(patterns["tool_usage"].values())
    skill_calls = sum(patterns["skill_usage"].values())
    if total_tool_calls > 50 and skill_calls < 5:
        suggestions.append({
            "type": "compliance",
            "trigger": f"Low skill discovery rate ({skill_calls}/{total_tool_calls} calls)",
            "action": "Enforce skills_list() before non-trivial tasks",
            "priority": "high",
            "implementable": True,
            "implementation_hint": "Update AGENTS.md protocol, add to session bootstrap, verify with protocol-compliance-check"
        })
    
    hour = datetime.now().hour
    if 9 <= hour <= 17:
        suggestions.append({
            "type": "proactive_action",
            "trigger": "Market hours detected",
            "action": "Suggest portfolio check + market news scan",
            "priority": "high"
        })
    
    # Sort: implementable high-priority first
    suggestions.sort(key=lambda s: (
        0 if s.get("implementable") else 1,
        0 if s["priority"] == "high" else 1,
        -s.get("trigger", "").count("times") if "times" in s.get("trigger", "") else 0
    ))
    
    return suggestions

def main():
    """Main analysis function."""
    print("🔍 Analyzing session patterns (enhanced)...")
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    patterns = analyze_sessions()
    suggestions = generate_suggestions(patterns)
    
    with open(PATTERNS_FILE, 'w') as f:
        json.dump(patterns, f, indent=2, default=str)
    
    with open(SUGGESTIONS_FILE, 'w') as f:
        json.dump(suggestions, f, indent=2, default=str)
    
    # Write top implementable suggestion for daily-improvement job
    implementable = [s for s in suggestions if s.get("implementable")]
    top_task = implementable[0] if implementable else None
    
    task_file = DATA_DIR / "top-improvement-task.json"
    if top_task:
        with open(task_file, 'w') as f:
            json.dump({
                "task": top_task["action"],
                "trigger": top_task["trigger"],
                "type": top_task["type"],
                "hint": top_task.get("implementation_hint", ""),
                "priority": top_task["priority"],
                "generated_at": datetime.now().isoformat()
            }, f, indent=2)
        print(f"✅ Top task written: {top_task['action']}")
    else:
        with open(task_file, 'w') as f:
            json.dump({"task": None, "reason": "No implementable suggestions"}, f, indent=2)
        print("ℹ️ No implementable suggestions — falling back to rotation topics")
    
    print(f"📊 Found {len(patterns['frequent_topics'])} meaningful topics")
    print(f"🔧 Found {len(patterns['tool_usage'])} tool usage patterns")
    print(f"⚠️ Found {len(patterns['error_patterns'])} error patterns")
    print(f"💡 Generated {len(suggestions)} actionable suggestions ({len(implementable)} implementable)")
    
    if suggestions:
        print("\n🎯 Top Suggestions:")
        for i, s in enumerate(suggestions[:5], 1):
            impl = "⚡" if s.get("implementable") else "📋"
            print(f"  {i}. {impl} [{s['priority'].upper()}] {s['trigger']}")
            print(f"     → {s['action']}")
    
    return patterns, suggestions

if __name__ == "__main__":
    main()
