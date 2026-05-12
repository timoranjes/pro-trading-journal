#!/usr/bin/env python3
"""Calculate hours remaining until US market open (9:30 AM ET) from current HKT time.
Accounts for US Daylight Saving Time transitions.
"""
from datetime import datetime, timezone, timedelta
import calendar

def get_us_dst_range(year):
    """Return (dst_start, dst_end) as naive datetimes.
    DST starts: 2nd Sunday of March at 2:00 AM local
    DST ends: 1st Sunday of November at 2:00 AM local
    """
    # 2nd Sunday of March
    mar1 = datetime(year, 3, 1)
    day_of_week = mar1.weekday()  # Monday=0
    days_until_sunday = (6 - day_of_week) % 7
    second_sunday = mar1 + timedelta(days=days_until_sunday + 7)

    # 1st Sunday of November
    nov1 = datetime(year, 11, 1)
    day_of_week = nov1.weekday()
    days_until_sunday = (6 - day_of_week) % 7
    first_sunday = nov1 + timedelta(days=days_until_sunday)

    return second_sunday, first_sunday

def main():
    now = datetime.now(timezone(timedelta(hours=8)))  # HKT
    year = now.year
    dst_start, dst_end = get_us_dst_range(year)

    # Determine if we're in DST
    naive_now = now.replace(tzinfo=None)
    is_dst = dst_start <= naive_now < dst_end

    # Market open in HKT
    if is_dst:
        open_hour = 21  # 9:30 AM ET = 9:30 PM HKT (UTC-4 vs UTC+8 = 12h diff)
    else:
        open_hour = 22  # 9:30 AM ET = 10:30 PM HKT (UTC-5 vs UTC+8 = 13h diff)

    market_open = now.replace(hour=open_hour, minute=30, second=0, microsecond=0)

    # If already past market open today, show next session
    if now >= market_open:
        market_open = market_open + timedelta(days=1)

    remaining_hours = (market_open - now).total_seconds() / 3600

    if remaining_hours < 1:
        remaining_mins = int(remaining_hours * 60)
        print(f"{remaining_mins}分钟")
    else:
        hours = int(remaining_hours)
        mins = int((remaining_hours - hours) * 60)
        if mins >= 30:
            print(f"约{hours + 1}小时")
        else:
            print(f"约{hours}小时")

if __name__ == "__main__":
    main()
