#!/usr/bin/env python3
"""
Shared utility for HKS cash monitor scripts.
Tracks sent emails by Message-ID to prevent duplicate deliveries.
Uses Discord webhook for delivery.
"""
import sys, os, json, hashlib, requests, email
from pathlib import Path
from datetime import datetime, timedelta, timezone

SENT_STATE_PATH = Path.home() / '.hermes' / 'data' / 'hks_cash_sent.json'
WEBHOOK_URL = "https://discord.com/api/webhooks/1501536406516793415/3fSCHdHPCdookn05uN_hJOhgNXfv-zLigzrpIRE_86BIMTnw91d3JVLppoV8GRLU_8Gh"

def load_sent_state():
    """Load sent email tracking state."""
    if SENT_STATE_PATH.exists():
        return json.loads(SENT_STATE_PATH.read_text())
    return {}

def save_sent_state(state):
    """Save sent email tracking state."""
    SENT_STATE_PATH.write_text(json.dumps(state, indent=2))

def get_today_key():
    """Get today's date string for state tracking."""
    return datetime.now().strftime("%Y-%m-%d")

def is_already_sent(message_id):
    """Check if this email was already sent today."""
    state = load_sent_state()
    today = get_today_key()
    if today in state:
        return message_id in state[today]
    return False

def mark_sent(message_id):
    """Mark this email as sent today."""
    state = load_sent_state()
    today = get_today_key()
    if today not in state:
        state[today] = []
    state[today].append(message_id)
    # Keep only last 7 days to prevent bloat
    cutoff = (datetime.now() - __import__('datetime').timedelta(days=7)).strftime("%Y-%m-%d")
    state = {k: v for k, v in state.items() if k >= cutoff}
    save_sent_state(state)

def send_via_webhook(content, username="HKS Cash Monitor"):
    """Send content to Discord via webhook. Returns True if successful."""
    if not content or not content.strip():
        return False
    
    # Split if too long for Discord
    max_len = 1990
    chunks = [content[i:i+max_len] for i in range(0, len(content), max_len)]
    
    for chunk in chunks:
        r = requests.post(WEBHOOK_URL, json={
            'content': chunk,
            'username': username
        }, timeout=10)
        if r.status_code not in (200, 204):
            print(f"⚠️ Webhook failed: HTTP {r.status_code}", file=sys.stderr)
            return False
    return True

def send_header_via_webhook(subject, email_date_str):
    """Send email subject + timestamp as a separate header message."""
    # Parse and format the email date to HKT
    try:
        dt = email.utils.parsedate_to_datetime(email_date_str)
        # Convert to HKT (UTC+8)
        from datetime import timezone
        hkt = dt.astimezone(timezone(timedelta(hours=8)))
        formatted_time = hkt.strftime("%Y-%m-%d %H:%M HKT")
    except:
        formatted_time = email_date_str
    
    header = f"📧 **{subject}**\n🕐 {formatted_time}"
    return send_via_webhook(header, username="HKS Cash Header")

def get_message_id(msg):
    """Extract Message-ID from email message object."""
    mid = msg.get("Message-ID", "")
    if not mid:
        # Fallback: hash the subject + date
        subject = msg.get("Subject", "")
        date = msg.get("Date", "")
        mid = hashlib.md5(f"{subject}{date}".encode()).hexdigest()
    return mid
