"""NYSE calendar and session clock. Stdlib only.

The holiday rules are copied from autorobin/market_calendar.py so this repo can be
published without the rest of AutoRobin. One-off closures are not known here.
"""
import datetime as dt

SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)


def _nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    next_month = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year):
    # Anonymous Gregorian algorithm.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(day):
    # Saturday moves to Friday, Sunday moves to Monday.
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def holidays(year):
    days = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(dt.date(year, 6, 19)),  # Juneteenth
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(dt.date(year, 12, 25)),  # Christmas
    }
    # NYSE does not observe New Year's Day on the prior Friday when Jan 1 is a Saturday.
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:
        days.add(_observed(new_year))
    return days


def is_trading_day(day):
    return day.weekday() < 5 and day not in holidays(day.year)


def trading_days_between(start, end):
    """Trading days in (start, end]. Negative when end is before start."""
    if end < start:
        return -trading_days_between(end, start)
    count = 0
    day = start
    while day < end:
        day += dt.timedelta(days=1)
        if is_trading_day(day):
            count += 1
    return count


def to_eastern(utc_now):
    """Convert an aware UTC datetime to naive Eastern time, using the US DST rule."""
    year = utc_now.year
    # DST runs from 2am on the second Sunday of March to 2am on the first Sunday of November.
    dst_start = dt.datetime.combine(_nth_weekday(year, 3, 6, 2), dt.time(7), dt.timezone.utc)
    dst_end = dt.datetime.combine(_nth_weekday(year, 11, 6, 1), dt.time(6), dt.timezone.utc)
    offset = -4 if dst_start <= utc_now < dst_end else -5
    return (utc_now + dt.timedelta(hours=offset)).replace(tzinfo=None)


def close_time(day):
    july_4 = dt.date(day.year, 7, 4)
    christmas = dt.date(day.year, 12, 25)
    if day == _nth_weekday(day.year, 11, 3, 4) + dt.timedelta(days=1):
        return EARLY_CLOSE
    if day == dt.date(day.year, 7, 3) and july_4.weekday() < 5:
        return EARLY_CLOSE
    if day == dt.date(day.year, 12, 24) and christmas.weekday() < 5:
        return EARLY_CLOSE
    return SESSION_CLOSE


def is_open(eastern_now):
    day = eastern_now.date()
    if not is_trading_day(day):
        return False
    return SESSION_OPEN <= eastern_now.time() < close_time(day)


def session_fraction(eastern_now):
    """Share of today's session that has elapsed, or 1.0 when the market is closed."""
    if not is_open(eastern_now):
        return 1.0
    day = eastern_now.date()
    start = dt.datetime.combine(day, SESSION_OPEN)
    end = dt.datetime.combine(day, close_time(day))
    return (eastern_now - start).total_seconds() / (end - start).total_seconds()
