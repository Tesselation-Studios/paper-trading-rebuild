"""
Market Hours and Trading Holidays

Tracks when US stock market (NYSE) is open/closed.
Prevents bot from trading when market is closed.

Key facts:
- NYSE hours: 9:30 AM - 4:00 PM ET, Monday-Friday
- Closed on federal holidays
- Early close (2:00 PM) on day before Thanksgiving and Christmas Eve
- No pre-market trading in our system (though real trading has 4 AM - 9:30 AM)

Usage:
    from src.market_hours import is_market_open, days_until_market_open

    if not is_market_open():
        print("Market is closed. Skipping trades.")
        return

Author: Claude Code
Date: 2026-05-20
"""

from datetime import datetime, timedelta
from typing import List, Tuple, Optional
from zoneinfo import ZoneInfo

# ============================================================================
# CONSTANTS
# ============================================================================

# US Eastern Time (where NYSE trades)
ET = ZoneInfo("America/New_York")

# Regular market hours: 9:30 AM - 4:00 PM ET, Monday-Friday
MARKET_OPEN_TIME = 9, 30  # (hours, minutes) in 24-hour format
MARKET_CLOSE_TIME = 16, 0  # 4:00 PM

# Early close times (2:00 PM on certain days)
EARLY_CLOSE_TIME = 14, 0  # 2:00 PM


# ============================================================================
# HOLIDAY DEFINITIONS
# ============================================================================

def get_fixed_holidays() -> List[Tuple[int, int]]:
    """
    Get holidays that occur on the same date every year.

    Returns:
        List of (month, day) tuples
        Examples: (1, 1) for Jan 1, (7, 4) for July 4

    NYSE Closed (Full Day):
        - New Year's Day: Jan 1
        - Independence Day: July 4
        - Thanksgiving: 4th Thursday in Nov (handled separately)
        - Christmas: Dec 25
    """
    return [
        (1, 1),    # New Year's Day
        (7, 4),    # Independence Day
        (12, 25),  # Christmas
    ]


def get_floating_holidays(year: int) -> List[datetime]:
    """
    Get holidays that move around each year (3rd Monday, 4th Thursday, etc.).

    Args:
        year: Year to calculate holidays for

    Returns:
        List of datetime objects

    NYSE Closed (Full Day):
        - MLK Jr Day: 3rd Monday in January
        - Presidents Day: 3rd Monday in February
        - Memorial Day: Last Monday in May
        - Juneteenth: June 19 (observed June 19 or nearest weekday)
        - Labor Day: 1st Monday in September
        - Thanksgiving: 4th Thursday in November

    Note: Juneteenth was added to NYSE holidays in 2022.
    """
    holidays = []

    # MLK Jr Day (3rd Monday in January)
    mlk_day = _get_nth_weekday(year, 1, 0, 3)  # Jan, Monday, 3rd
    holidays.append(mlk_day)

    # Presidents Day (3rd Monday in February)
    presidents_day = _get_nth_weekday(year, 2, 0, 3)  # Feb, Monday, 3rd
    holidays.append(presidents_day)

    # Good Friday (2 days before Easter - calculated dynamically)
    easter = _calculate_easter(year)
    good_friday = easter - timedelta(days=2)
    holidays.append(good_friday)

    # Memorial Day (last Monday in May)
    memorial_day = _get_last_weekday(year, 5, 0)  # May, Monday
    holidays.append(memorial_day)

    # Juneteenth (June 19, or nearest weekday if weekend)
    juneteenth = datetime(year, 6, 19)
    # If Juneteenth falls on weekend, NYSE observes on nearest weekday
    if juneteenth.weekday() == 5:  # Saturday
        juneteenth = datetime(year, 6, 18)  # Friday before
    elif juneteenth.weekday() == 6:  # Sunday
        juneteenth = datetime(year, 6, 20)  # Monday after
    holidays.append(juneteenth)

    # Labor Day (1st Monday in September)
    labor_day = _get_nth_weekday(year, 9, 0, 1)  # Sept, Monday, 1st
    holidays.append(labor_day)

    # Thanksgiving (4th Thursday in November)
    thanksgiving = _get_nth_weekday(year, 11, 3, 4)  # Nov, Thursday, 4th
    holidays.append(thanksgiving)

    return holidays


def get_early_close_days(year: int) -> List[datetime]:
    """
    Get days when market closes early (2:00 PM instead of 4:00 PM).

    Args:
        year: Year to calculate for

    Returns:
        List of datetime objects

    Early Close Days:
        - Day after Thanksgiving (Friday)
        - Christmas Eve (Dec 24), if it's a weekday
    """
    early_closes = []

    # Day after Thanksgiving (Friday)
    thanksgiving = _get_nth_weekday(year, 11, 3, 4)
    day_after = thanksgiving + timedelta(days=1)
    if day_after.weekday() < 5:  # If it's a weekday
        early_closes.append(day_after)

    # Christmas Eve (Dec 24), only if weekday
    christmas_eve = datetime(year, 12, 24)
    if christmas_eve.weekday() < 5:  # Monday-Friday only
        early_closes.append(christmas_eve)

    return early_closes


# ============================================================================
# HELPER FUNCTIONS: Holiday Calculations
# ============================================================================

def _get_nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """
    Get the nth occurrence of a weekday in a month.

    Args:
        year: Year
        month: Month (1-12)
        weekday: Weekday (0=Monday, 6=Sunday)
        n: Which occurrence (1 = first, 2 = second, etc.)

    Returns:
        datetime object

    Example:
        3rd Monday in January:
        _get_nth_weekday(2026, 1, 0, 3)
    """
    # Start with the first day of the month
    date = datetime(year, month, 1)

    # Find how many days until the first occurrence of our weekday
    days_until = (weekday - date.weekday()) % 7

    # Move to first occurrence
    date = date + timedelta(days=days_until)

    # Move to the nth occurrence
    date = date + timedelta(weeks=n - 1)

    return date


def _get_last_weekday(year: int, month: int, weekday: int) -> datetime:
    """
    Get the last occurrence of a weekday in a month.

    Args:
        year: Year
        month: Month (1-12)
        weekday: Weekday (0=Monday, 6=Sunday)

    Returns:
        datetime object

    Example:
        Last Monday in May (Memorial Day):
        _get_last_weekday(2026, 5, 0)
    """
    # Start with last day of the month
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)

    last_day = next_month - timedelta(days=1)

    # Find how many days back to our weekday
    days_back = (last_day.weekday() - weekday) % 7

    return last_day - timedelta(days=days_back)


def _calculate_easter(year: int) -> datetime:
    """
    Calculate Easter Sunday for a given year (Computus algorithm).

    Used to calculate Good Friday (2 days before Easter).

    Args:
        year: Year

    Returns:
        datetime of Easter Sunday
    """
    # Simplified Easter calculation
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1

    return datetime(year, month, day)


# ============================================================================
# MAIN FUNCTIONS: Market Status
# ============================================================================

def is_after_hours(check_time: Optional[datetime] = None) -> bool:
    """
    Check if the market is currently in after-hours (closed but weekday).

    Returns True when the market is closed but it's a weekday that is not
    a holiday — i.e., before 9:30 AM or after 4:00 PM (or 2:00 PM on early
    close days) on a regular trading day.

    Returns False when the market is open, or on weekends/holidays.

    Args:
        check_time: datetime to check. If None, checks current time.

    Returns:
        True if after-hours (weekday, not holiday, outside trading hours),
        False otherwise.
    """
    if check_time is None:
        check_time = datetime.now(ET)
    else:
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=ET)
        else:
            check_time = check_time.astimezone(ET)

    # Must be a weekday
    if check_time.weekday() >= 5:
        return False

    # Must NOT be a holiday
    if is_holiday(check_time.date()):
        return False

    # Must be outside trading hours
    hour, minute = check_time.hour, check_time.minute
    current_time = (hour, minute)

    if is_early_close_day(check_time.date()):
        close_time = EARLY_CLOSE_TIME
    else:
        close_time = MARKET_CLOSE_TIME

    # After-hours = before open OR at/after close
    return current_time < MARKET_OPEN_TIME or current_time >= close_time


def is_market_open(check_time: Optional[datetime] = None) -> bool:
    """
    Check if the market is currently open (or open at a specific time).

    Args:
        check_time: datetime to check. If None, checks current time.
                   Should be in any timezone; will be converted to ET.

    Returns:
        True if market is open, False if closed

    Logic:
        Market is open if:
        1. It's a weekday (Mon-Fri)
        2. Not a holiday
        3. Between 9:30 AM and 4:00 PM ET
        (or 9:30 AM and 2:00 PM on early close days)
    """
    if check_time is None:
        check_time = datetime.now(ET)
    else:
        # Convert to ET if naive datetime
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=ET)
        else:
            check_time = check_time.astimezone(ET)

    # Check if weekday
    if check_time.weekday() >= 5:  # Saturday or Sunday
        return False

    # Check if holiday
    if is_holiday(check_time.date()):
        return False

    # Check time of day
    hour, minute = check_time.hour, check_time.minute
    current_time = (hour, minute)

    if is_early_close_day(check_time.date()):
        close_time = EARLY_CLOSE_TIME
    else:
        close_time = MARKET_CLOSE_TIME

    return MARKET_OPEN_TIME <= current_time < close_time


def is_holiday(date_to_check) -> bool:
    """
    Check if a specific date is a trading holiday.

    Args:
        date_to_check: datetime.date object

    Returns:
        True if market is closed, False if open
    """
    # Check fixed holidays
    for month, day in get_fixed_holidays():
        if date_to_check.month == month and date_to_check.day == day:
            return True

    # Check floating holidays
    floating = get_floating_holidays(date_to_check.year)
    for holiday in floating:
        if date_to_check == holiday.date():
            return True

    return False


def is_early_close_day(date_to_check) -> bool:
    """
    Check if market closes early (2:00 PM) on this date.

    Args:
        date_to_check: datetime.date object

    Returns:
        True if early close, False if regular hours
    """
    early_closes = get_early_close_days(date_to_check.year)
    return any(ec.date() == date_to_check for ec in early_closes)


def time_until_market_open(from_time: Optional[datetime] = None) -> timedelta:
    """
    Get how long until the market opens.

    Args:
        from_time: datetime to calculate from. If None, uses current time.

    Returns:
        timedelta object

    Example:
        If called on Saturday at noon:
        Returns: timedelta(hours=21.5)  # Until Monday 9:30 AM
    """
    if from_time is None:
        from_time = datetime.now(ET)
    else:
        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=ET)
        else:
            from_time = from_time.astimezone(ET)

    # If market is open, return 0
    if is_market_open(from_time):
        return timedelta(0)

    # Find next market open time
    check_time = from_time + timedelta(minutes=1)

    # Search forward up to 5 days
    for _ in range(5 * 24 * 60):
        if is_market_open(check_time):
            return check_time - from_time
        check_time += timedelta(minutes=1)

    # Fallback (shouldn't happen)
    return timedelta(days=5)


def next_market_open(from_time: Optional[datetime] = None) -> datetime:
    """
    Get the exact time when market will next open.

    Args:
        from_time: datetime to calculate from. If None, uses current time.

    Returns:
        datetime object in ET timezone

    Example:
        If called on Friday 4:30 PM ET:
        Returns: Monday 9:30 AM ET
    """
    if from_time is None:
        from_time = datetime.now(ET)
    else:
        if from_time.tzinfo is None:
            from_time = from_time.replace(tzinfo=ET)
        else:
            from_time = from_time.astimezone(ET)

    check_time = from_time.replace(hour=9, minute=30, second=0, microsecond=0)

    # If we're before market open today and today is open, return today's open
    if check_time > from_time and is_market_open(check_time):
        return check_time

    # Otherwise, move to next day and find first open day
    check_time = check_time + timedelta(days=1)
    while not is_market_open(check_time):
        check_time = check_time + timedelta(days=1)
        if (check_time - from_time).days > 10:  # Sanity check
            break

    return check_time


# ============================================================================
# USER CONFIGURATION: Custom Holidays
# ============================================================================

def load_custom_holidays() -> List[str]:
    """
    Load custom holidays from state/custom-holidays.txt.

    Format: One date per line, YYYY-MM-DD format
    Example:
        2026-12-26
        2026-12-27

    Returns:
        List of date strings (YYYY-MM-DD)
    """
    from pathlib import Path

    holiday_file = Path("state/custom-holidays.txt")

    if not holiday_file.exists():
        return []

    with open(holiday_file, "r") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    return lines


def is_custom_holiday(date_to_check) -> bool:
    """
    Check if date is in custom holidays list.

    Args:
        date_to_check: datetime.date object

    Returns:
        True if in custom holidays, False otherwise
    """
    custom = load_custom_holidays()
    date_str = date_to_check.strftime("%Y-%m-%d")
    return date_str in custom


def add_custom_holiday(date_to_add: str) -> None:
    """
    Add a custom holiday to state/custom-holidays.txt.

    Args:
        date_to_add: String in YYYY-MM-DD format

    Example:
        add_custom_holiday("2026-12-26")  # Extra day off
    """
    from pathlib import Path

    holiday_file = Path("state/custom-holidays.txt")

    # Create file if doesn't exist
    Path("state").mkdir(exist_ok=True)
    if not holiday_file.exists():
        holiday_file.write_text(
            "# Custom holidays (dates when you want to skip trading)\n"
            "# Format: YYYY-MM-DD\n"
            "# Example:\n"
            "# 2026-12-26\n"
            "# 2026-12-27\n"
            "\n"
        )

    # Append date if not already there
    with open(holiday_file, "r") as f:
        existing = f.read()

    if date_to_add not in existing:
        with open(holiday_file, "a") as f:
            f.write(f"{date_to_add}\n")


# ============================================================================
# TESTING / EXAMPLES
# ============================================================================

if __name__ == "__main__":
    """
    Test market hours functionality.

    Try:
        python3 src/market_hours.py
    """
    print("Testing market hours...\n")

    # Test current status
    now = datetime.now(ET)
    print(f"Current time (ET): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Is market open? {is_market_open()}\n")

    # Test specific dates
    test_dates = [
        "2026-01-01",  # New Year's Day (holiday)
        "2026-01-05",  # Monday (should be open if time is 10 AM)
        "2026-01-09",  # Friday (MLK Day - Monday, but test day after)
        "2026-11-26",  # Thanksgiving (holiday)
        "2026-11-27",  # Day after (early close)
        "2026-12-25",  # Christmas (holiday)
        "2026-12-24",  # Christmas Eve (early close)
    ]

    print("Holiday checks:")
    for date_str in test_dates:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        is_hol = is_holiday(date_obj)
        is_early = is_early_close_day(date_obj)
        status = "HOLIDAY" if is_hol else ("EARLY CLOSE" if is_early else "REGULAR")
        print(f"  {date_str}: {status}")

    print("\n")

    # Test time until open
    until_open = time_until_market_open()
    print(f"Time until market opens: {until_open}")
    next_open = next_market_open()
    print(f"Next market open: {next_open.strftime('%Y-%m-%d %H:%M:%S')}")

    print("\nDone!")
