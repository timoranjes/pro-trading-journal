#!/usr/bin/env python3
"""EOD HKS Cash - Content body only."""
import sys, subprocess, imaplib, email, re
from email.header import decode_header
from datetime import datetime, timedelta

PASSWORD_CMD = ["security", "find-generic-password", "-s", "com.clawdbot.cicc-alimail", "-w"]
IMAP_HOST = "imap.harmolands.com"
SENDER_JESSIE = "zhengjy@harmolands.com"

def get_password():
    return subprocess.run(PASSWORD_CMD, capture_output=True, text=True).stdout.strip()

def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)

def get_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                b = part.get_payload(decode=True)
                if b: return b.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        b = msg.get_payload(decode=True)
        if b: return b.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""

def fetch_email_by_subject(folder, subject_kw, sender=None, days_back=3):
    password = get_password()
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login("liaozc@harmolands.com", password)
    mail.select(folder)
    status, messages = mail.search(None, "ALL")
    ids = messages[0].split() if messages[0] else []
    cutoff = datetime.now() - timedelta(days=days_back)
    candidates = []
    for num in reversed(ids[-50:]):
        try:
            status, msg_data = mail.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]: continue
            raw = msg_data[0][1]
            if not raw: continue
            msg = email.message_from_bytes(raw)
            date_str = msg.get("Date", "")
            try:
                email_date = email.utils.parsedate_to_datetime(date_str).replace(tzinfo=None)
            except:
                email_date = datetime.min
            if email_date < cutoff: continue
            if sender:
                from_addr = decode_str(msg.get("From", ""))
                if sender not in from_addr: continue
            subject = decode_str(msg.get("Subject", ""))
            if subject_kw.upper() in subject.upper():
                body = get_email_body(msg)
                candidates.append((email_date, subject, body))
        except: continue
    mail.logout()
    candidates.sort(reverse=True)
    return candidates[0] if candidates else None

def parse_eod_email(body):
    hks_match = re.search(r"HKS available\s*(?:USD\s*)?(\d+\.?\d*)\s*M", body, re.IGNORECASE)
    ftml_match = re.search(r"FTL balance\s*(?:USD\s*)?(\d+\.?\d*)\s*M", body, re.IGNORECASE)
    release_match = re.search(r"additional\s*(?:USD\s*)?(\d+\.?\d*)\s*M", body, re.IGNORECASE)
    hks_available = hks_match.group(1) + "M" if hks_match else "N/A"
    ftml_balance = ftml_match.group(1) + "M" if ftml_match else "N/A"
    release_needed = release_match.group(1) + "M" if release_match else "N/A"
    return hks_available, ftml_balance, release_needed

def get_jesse_nav_report(folder, days_back=1):
    password = get_password()
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login("liaozc@harmolands.com", password)
    mail.select(folder)
    cutoff = datetime.now() - timedelta(days=days_back)
    candidates = []
    status, messages = mail.search(None, "ALL")
    ids = messages[0].split() if messages[0] else []
    for num in reversed(ids[-50:]):
        try:
            status, msg_data = mail.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]: continue
            raw = msg_data[0][1]
            if not raw: continue
            msg = email.message_from_bytes(raw)
            date_str = msg.get("Date", "")
            try:
                email_date = email.utils.parsedate_to_datetime(date_str).replace(tzinfo=None)
            except:
                email_date = datetime.min
            if email_date < cutoff: continue
            from_addr = decode_str(msg.get("From", ""))
            if SENDER_JESSIE not in from_addr: continue
            subject = decode_str(msg.get("Subject", ""))
            if "净值日报" not in subject: continue
            body = get_email_body(msg)
            match = re.search(r"总仓位\s*(\d+\.?\d*)%", body)
            if match:
                total_pct = float(match.group(1))
                candidates.append((email_date, subject, body, total_pct))
        except: continue
    mail.logout()
    candidates.sort(reverse=True)
    return candidates[0] if candidates else None

def strip_email_footer(body):
    """Strip quoted previous email / footer from the body."""
    for pattern in [
        r"\n-{3,}\s*\nFrom:",
        r"\n> From:",
        r"\n-----Original Message-----",
        r"\n发件人:",
    ]:
        idx = re.search(pattern, body)
        if idx:
            return body[:idx.start()].rstrip()
    return body.strip()


if __name__ == "__main__":
    from hks_cash_utils import is_already_sent, mark_sent, send_via_webhook, send_header_via_webhook, get_message_id
    
    password = get_password()
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login("liaozc@harmolands.com", password)
    mail.select("INBOX")
    status, messages = mail.search(None, "ALL")
    ids = messages[0].split() if messages[0] else []
    cutoff = datetime.now() - timedelta(days=1)
    
    found = False
    for num in reversed(ids[-50:]):
        try:
            status, msg_data = mail.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]: continue
            raw = msg_data[0][1]
            if not raw: continue
            msg = email.message_from_bytes(raw)
            date_str = msg.get("Date", "")
            try:
                email_date = email.utils.parsedate_to_datetime(date_str).replace(tzinfo=None)
            except:
                email_date = datetime.min
            if email_date < cutoff: continue
            from_addr = decode_str(msg.get("From", ""))
            if SENDER_JESSIE not in from_addr: continue
            subject = decode_str(msg.get("Subject", ""))
            if "EOD HKT" not in subject.upper(): continue
            
            # Only match TODAY's date in subject (e.g. "2026/5/7")
            today_str = datetime.now().strftime("%Y/%-m/%-d").replace(" 0", " ").lstrip("0")
            if today_str not in subject: continue
            
            body = get_email_body(msg)
            message_id = get_message_id(msg)
            if is_already_sent(message_id):
                mail.logout()
                sys.exit(0)
            
            # Send header first (subject + timestamp)
            send_header_via_webhook(subject, date_str)
            
            # Then send content
            clean_body = strip_email_footer(body)
            nav_output = ""
            nav_data = get_jesse_nav_report("INBOX", days_back=1)
            if nav_data:
                _, _, _, total_pct = nav_data
                if total_pct > 100:
                    nav_output = f"\n总仓位 {total_pct:.1f}%"
            
            content = clean_body + nav_output
            send_via_webhook(content, username="EOD Content")
            mark_sent(message_id)
            found = True
            break
        except: continue
    try:
        mail.logout()
    except:
        pass
    if not found:
        sys.exit(0)
