#!/usr/bin/env python3
"""resource_watchdog.py — Monitor disk and memory usage with auto-cleanup.
Delivers alert ONLY when thresholds breached (watchdog pattern).
Performs automatic cleanup of safe targets before reporting.
"""
import shutil
import sys
import os
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

import psutil

DISK_WARN_PCT = 80
DISK_CRIT_PCT = 90
MEM_WARN_PCT = 85
MEM_CRIT_PCT = 95

alerts = []
cleanup_actions = []

def run_cmd(cmd, timeout=30):
    """Run a command with timeout, return (success, output)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False, ""

def cleanup_cron_output():
    """Clean cron output files older than 7 days."""
    cron_dir = Path.home() / '.hermes' / 'cron' / 'output'
    if not cron_dir.exists():
        return
    success, output = run_cmd([
        'find', str(cron_dir), '-type', 'f', '-mtime', '+7', '-delete', '-print'
    ], timeout=15)
    if success and output:
        count = len(output.strip().split('\n'))
        cleanup_actions.append(f"Cleaned {count} old cron output files")

def cleanup_tmp():
    """Clean temp files older than 7 days."""
    success, output = run_cmd([
        'find', '/tmp', '-type', 'f', '-mtime', '+7', '-delete', '-print'
    ], timeout=15)
    if success and output:
        count = len(output.strip().split('\n'))
        cleanup_actions.append(f"Cleaned {count} old temp files")

def cleanup_pip_cache():
    """Clean pip cache."""
    success, _ = run_cmd(['pip', 'cache', 'purge'], timeout=30)
    if success:
        cleanup_actions.append("Purged pip cache")

def cleanup_npm_cache():
    """Clean npm cache."""
    success, _ = run_cmd(['npm', 'cache', 'clean', '--force'], timeout=30)
    if success:
        cleanup_actions.append("Cleaned npm cache")

def cleanup_pycache():
    """Clean Python __pycache__ in hermes directory."""
    hermes_dir = Path.home() / '.hermes'
    if not hermes_dir.exists():
        return
    success, output = run_cmd([
        'find', str(hermes_dir), '-type', 'd', '-name', '__pycache__', '-exec', 'rm', '-rf', '{}', '+'
    ], timeout=30)
    if success:
        cleanup_actions.append("Cleaned Python __pycache__ files")

def empty_trash():
    """Empty macOS Trash."""
    trash_dir = Path.home() / '.Trash'
    if not trash_dir.exists():
        return
    success, _ = run_cmd(['osascript', '-e', 'empty trash'], timeout=30)
    if success:
        cleanup_actions.append("Emptied macOS Trash")

def cleanup_old_cache():
    """Clean old cache files (>30 days) in ~/.cache."""
    cache_dir = Path.home() / '.cache'
    if not cache_dir.exists():
        return
    success, output = run_cmd([
        'find', str(cache_dir), '-type', 'f', '-mtime', '+30', '-delete', '-print'
    ], timeout=60)
    if success and output:
        count = len(output.strip().split('\n'))
        cleanup_actions.append(f"Cleaned {count} old cache files (>30 days)")

def cleanup_huggingface():
    """Clean old HuggingFace cache (>90 days) - only when disk critically low."""
    disk = shutil.disk_usage("/")
    disk_pct = (disk.used / disk.total) * 100
    if disk_pct < DISK_CRIT_PCT:
        return  # Only clean when critically low
    
    hf_dir = Path.home() / '.cache' / 'huggingface'
    if not hf_dir.exists():
        return
    success, output = run_cmd([
        'find', str(hf_dir), '-type', 'f', '-mtime', '+90', '-delete', '-print'
    ], timeout=60)
    if success and output:
        count = len(output.strip().split('\n'))
        cleanup_actions.append(f"Cleaned {count} old HuggingFace files (>90 days)")

def get_disk_breakdown():
    """Get disk usage breakdown for LLM analysis."""
    disk = shutil.disk_usage("/")
    disk_pct = (disk.used / disk.total) * 100
    disk_gb_free = disk.free / (1024**3)
    
    # Major directories
    dirs = {}
    for d in ['~/.cache', '~/.hermes', '~/Library/Caches', '/tmp', '~/.Trash']:
        p = Path(d).expanduser()
        if p.exists():
            size = sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
            dirs[d] = f"{size/(1024**3):.2f} GB"
    
    return {
        "total_gb": f"{disk.total/(1024**3):.0f}",
        "used_gb": f"{disk.used/(1024**3):.1f}",
        "free_gb": f"{disk_gb_free:.1f}",
        "pct": f"{disk_pct:.1f}",
        "dirs": dirs
    }

# Run auto-cleanup
cleanup_cron_output()
cleanup_tmp()
cleanup_pip_cache()
cleanup_npm_cache()
cleanup_pycache()
empty_trash()
cleanup_old_cache()
cleanup_huggingface()

# Re-check disk after cleanup
disk = shutil.disk_usage("/")
disk_pct = (disk.used / disk.total) * 100
disk_gb_free = disk.free / (1024**3)

if disk_pct >= DISK_CRIT_PCT:
    alerts.append(f"🔴 CRITICAL: Disk {disk_pct:.1f}% full ({disk_gb_free:.1f} GB free)")
elif disk_pct >= DISK_WARN_PCT:
    alerts.append(f"⚠️ WARNING: Disk {disk_pct:.1f}% full ({disk_gb_free:.1f} GB free)")

# Memory usage
mem = psutil.virtual_memory()
if mem.percent >= MEM_CRIT_PCT:
    alerts.append(f"🔴 CRITICAL: Memory {mem.percent:.1f}% used ({mem.available / (1024**3):.1f} GB free)")
elif mem.percent >= MEM_WARN_PCT:
    alerts.append(f"⚠️ WARNING: Memory {mem.percent:.1f}% used ({mem.available / (1024**3):.1f} GB free)")

# Check ~/.hermes directory size
hermes_size = 0
for dirpath, _, filenames in os.walk(os.path.expanduser("~/.hermes")):
    for f in filenames:
        try:
            hermes_size += os.path.getsize(os.path.join(dirpath, f))
        except OSError:
            pass
hermes_gb = hermes_size / (1024**3)
if hermes_gb > 5:
    alerts.append(f"📦 ~/.hermes is {hermes_gb:.1f} GB — consider cleanup")

# Build output
output_lines = []
if alerts:
    output_lines.append("Resource Watchdog Alert:")
    output_lines.extend(alerts)
    
    if cleanup_actions:
        output_lines.append("\n✅ Auto-cleanup performed:")
        output_lines.extend([f"  • {action}" for action in cleanup_actions])
    
    # Add recommendations
    output_lines.append("\n💡 Recommendations:")
    if disk_pct >= DISK_WARN_PCT:
        output_lines.append("  • Run 'brew cleanup' to remove old Homebrew packages")
        output_lines.append("  • Check large files: find ~ -type f -size +1G -exec ls -lh {} \\;")
        output_lines.append("  • Consider moving old data to external storage")
    if mem.percent >= MEM_WARN_PCT:
        output_lines.append("  • Close unused applications")
        output_lines.append("  • Check memory hogs: ps aux --sort=-%mem | head -10")

elif cleanup_actions:
    # Only report if cleanup was performed
    output_lines.append("Resource Watchdog — Cleanup Report:")
    output_lines.extend([f"  • {action}" for action in cleanup_actions])

if output_lines:
    print("\n".join(output_lines))
    sys.exit(0)
else:
    sys.exit(0)  # Silent on success
