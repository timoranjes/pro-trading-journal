#!/usr/bin/env python3
"""
Daily HWMR (Holy Word for Morning Revival) portion extractor.
Reads from the English HWMR PDF via OCR and outputs formatted content for Discord.

Usage: python3 hwmr_daily_portion.py [--date YYYY-MM-DD] [--pdf PATH]

PDF structure (2025 December Semiannual Training):
- Outline pages + daily portions interleaved
- Each week's daily portion: 2 pages per day × 6 days = 12 pages
- Week N starts at a known page offset (varies by training)
"""

import sys
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

# Try importing dependencies
try:
    import fitz  # PyMuPDF
    from PIL import Image
    import pytesseract
except ImportError as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

# Configuration
DEFAULT_PDF = os.path.expanduser(
    "/Users/zichengliao/clawd/0119_A4-2025-DST-HWMR-en.pdf"
)
STATE_FILE = os.path.expanduser("~/.hermes/data/hwmr_state.json")

# Training series config
# Anchor: Week 46 starts Monday May 4, 2026 (Message 10)
# The training has gaps (holidays, breaks) so simple date math doesn't work.
# We use an anchor + PDF search approach.
ANCHOR_WEEK = 46
ANCHOR_MONDAY = datetime(2026, 5, 4)
DAYS_PER_WEEK = 6
PAGES_PER_DAY = 2  # Each day's portion spans 2 pages


def get_current_week_and_day(target_date: datetime):
    """Calculate which training week and day (1-6) a date falls on.
    
    Uses an anchor week + calendar-week offset. The training runs Mon-Sat
    each week with occasional breaks. We calculate the calendar week offset
    from the anchor and search the PDF to verify the week exists.
    """
    weekday = target_date.weekday()  # 0=Monday, 5=Saturday, 6=Sunday
    if weekday == 6:  # Sunday - no training
        return None, None

    day_in_week = weekday + 1  # 1=Mon, 2=Tue, ..., 6=Sat

    # Calculate calendar week offset from anchor
    days_since_anchor = (target_date - ANCHOR_MONDAY).days
    week_offset = days_since_anchor // 7
    estimated_week = ANCHOR_WEEK + week_offset

    return estimated_week, day_in_week


def find_week_pages(doc, week_number: int):
    """
    Search the PDF for pages containing 'WEEK XX' and return the
    daily portion page range (not outline pages).
    """
    week_label = f"WEEK {week_number}"
    daily_pages = []

    for i in range(doc.page_count):
        text = doc[i].get_text()
        if week_label in text and "Holy Word Morning Revival" in text:
            daily_pages.append(i)

    if not daily_pages:
        return None

    # Return the exact daily portion pages (not the outline pages between them)
    return daily_pages


def ocr_page(doc, page_index: int) -> str:
    """Extract text from a PDF page using OCR (PyMuPDF + pytesseract)."""
    page = doc[page_index]
    mat = fitz.Matrix(3.0, 3.0)  # 3x resolution for better OCR
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    result = pytesseract.image_to_string(img, config="--psm 6")
    return result.strip()


def extract_daily_content(doc, week_pages: list, day_number: int) -> str:
    """Extract the full daily portion for a given day (1-6).
    
    week_pages contains the page index of the FIRST page for each day.
    Each day spans 2 consecutive pages.
    """
    if day_number < 1 or day_number > len(week_pages):
        return ""

    start_page = week_pages[day_number - 1]
    content_parts = []

    for offset in range(PAGES_PER_DAY):
        page_idx = start_page + offset
        if page_idx < doc.page_count:
            text = ocr_page(doc, page_idx)
            if text:
                content_parts.append(text)

    return "\n\n".join(content_parts)


def format_for_discord(content: str, week: int, day: int, day_name: str,
                       target_date: datetime) -> str:
    """Format the OCR'd content for Discord delivery."""
    # Extract the header info from content
    lines = content.split("\n")

    # Known HWMR section headers to bold
    SECTION_HEADERS = {
        "Morning Nourishment",
        "Today's Reading",
        "Today\u2019s Reading",  # curly apostrophe variant from OCR
        "Reading 1",
        "Reading 2",
        "Reading 3",
        "Reading 4",
        "Further Reading",
    }

    def is_section_header(line: str) -> bool:
        stripped = line.strip()
        # Exact match
        if stripped in SECTION_HEADERS:
            return True
        # "Further Reading: ..." variant
        if stripped.startswith("Further Reading"):
            return True
        return False

    # Build the message
    msg = f"📖 **Week {week} • {day_name}**\n"
    msg += f"**Series:** 2025 December Semiannual Training\n"
    msg += f"**Date:** {target_date.strftime('%B %d, %Y')} ({day_name})\n\n"

    # Add the full content (clean up OCR artifacts, preserve hard line breaks)
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip footer lines with page numbers
        if re.match(r"^2025.*Page \d+$", stripped):
            continue
        if re.match(r"^2025.*Week \d+.*Page \d+$", stripped):
            continue
        # Skip the header line (already in our format)
        if stripped.startswith(f"WEEK {week}"):
            continue
        # Bold section headers
        if is_section_header(line):
            clean_lines.append(f"**{stripped}**")
        else:
            clean_lines.append(line)

    # Preserve blank lines as paragraph separators for splitting
    # But collapse consecutive blank lines into single blank line
    result_lines = []
    prev_blank = False
    for line in clean_lines:
        is_blank = not line.strip()
        if is_blank:
            if not prev_blank and result_lines:
                result_lines.append("")
            prev_blank = True
        else:
            prev_blank = False
            result_lines.append(line)

    clean_content = "\n".join(result_lines).strip()
    msg += clean_content

    return msg


def send_to_webhook(webhook_url: str, content: str):
    """Send content to Discord via webhook, auto-splitting at 2000 chars.

    Splits at paragraph boundaries (\n\n). Only the FIRST chunk gets
    the header prepended; subsequent chunks are continuation-only.
    """
    import urllib.request
    import json

    MAX_CHARS = 2000
    if len(content) <= MAX_CHARS:
        _webhook_post(webhook_url, content)
        return

    header_end = content.find("\n\n")
    if header_end == -1:
        _webhook_post(webhook_url, content[:MAX_CHARS])
        return

    header = content[:header_end + 2]  # Include the \n\n
    body = content[header_end + 2:]

    paragraphs = body.split("\n\n")
    chunks = []
    current = ""
    first_chunk = True
    for para in paragraphs:
        # Determine what the full message would look like
        if first_chunk:
            candidate = (header + current + "\n\n" + para) if current else (header + para)
        else:
            candidate = (current + "\n\n" + para) if current else para

        if len(candidate) > MAX_CHARS and current:
            # Flush current chunk
            if first_chunk:
                chunks.append(header + current)
                first_chunk = False
            else:
                chunks.append(current)
            current = para
        else:
            if current:
                current = current + "\n\n" + para
            else:
                current = para
    if current:
        if first_chunk:
            chunks.append(header + current)
        else:
            chunks.append(current)

    for chunk in chunks:
        _webhook_post(webhook_url, chunk)


def _webhook_post(webhook_url: str, content: str):
    """Post a single message to Discord webhook."""
    import urllib.request
    import json

    payload = {"content": content}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HermesBot/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main():
    # Parse arguments
    target_date = datetime.now()
    pdf_path = DEFAULT_PDF

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            target_date = datetime.strptime(args[i + 1], "%Y-%m-%d")
            i += 2
        elif args[i] == "--pdf" and i + 1 < len(args):
            pdf_path = args[i + 1]
            i += 2
        else:
            i += 1

    # Determine week and day
    week, day = get_current_week_and_day(target_date)
    if week is None:
        print(f"No training content for {target_date.strftime('%Y-%m-%d')} (Sunday)",
              file=sys.stderr)
        sys.exit(0)  # Silent exit for Sunday

    day_name = target_date.strftime("%A")

    # Open PDF
    if not os.path.exists(pdf_path):
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doc = fitz.open(pdf_path)

    # Find the pages for this week's daily portion
    week_pages = find_week_pages(doc, week)
    if not week_pages:
        print(f"Could not find Week {week} in PDF", file=sys.stderr)
        sys.exit(1)

    # Extract content
    content = extract_daily_content(doc, week_pages, day)
    if not content:
        print(f"No content found for Week {week} Day {day}", file=sys.stderr)
        sys.exit(1)

    # Format for Discord
    formatted = format_for_discord(content, week, day, day_name, target_date)

    # Send via Discord webhook (direct delivery, bypassing gateway)
    WEBHOOK_CACHE_PATH = Path(STATE_FILE).parent / "webhooks.json"
    MORNING_REVIVAL_CHANNEL = "1499014389867614328"

    if WEBHOOK_CACHE_PATH.exists():
        with open(WEBHOOK_CACHE_PATH) as f:
            webhooks = json.load(f)
        webhook_url = webhooks.get(MORNING_REVIVAL_CHANNEL)
        if webhook_url:
            send_to_webhook(webhook_url, formatted)
            # Print nothing to stdout — webhook handles delivery
            # (cron no_agent=True: empty stdout = silent)
        else:
            print(f"No webhook found for channel {MORNING_REVIVAL_CHANNEL}",
                  file=sys.stderr)
            print(formatted)  # Fallback: print to stdout
    else:
        print(f"Webhook cache not found: {WEBHOOK_CACHE_PATH}", file=sys.stderr)
        print(formatted)  # Fallback: print to stdout

    # Update state file
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "last_week": week,
        "last_day": day,
        "last_date": target_date.strftime("%Y-%m-%d"),
        "training_id": "2025-12-DST",
        "pdf_path": pdf_path,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
