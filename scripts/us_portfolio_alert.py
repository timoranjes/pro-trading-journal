#!/usr/bin/env python3
"""US Portfolio Price Alerts — thin wrapper around portfolio_alert.py.

The main portfolio_alert.py handles ALL markets (US/HK/A) from the same
email source. The is_market_open() check inside it already filters to
only fetch prices for currently-open markets.

During US session hours (16:00-04:00 HKT):
  - US (.O) markets are open → prices fetched
  - HK (.HK) markets are closed → skipped
  - A-share (.SH/.SZ) markets are closed → skipped

So this script exists purely to satisfy the portfolio-llm-alerts cron
job prompt. It delegates entirely to portfolio_alert.py.
"""
import sys
from pathlib import Path

# Ensure parent directory is on sys.path for the import
sys.path.insert(0, str(Path(__file__).parent))

from portfolio_alert import main

import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-webhook", action="store_true",
                        help="Print alerts without sending Discord webhook")
    args = parser.parse_args()
    main(send_webhook=not args.no_webhook)
