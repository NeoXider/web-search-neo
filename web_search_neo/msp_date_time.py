import datetime


def format_region(moment: datetime.datetime) -> str:
    """``<zone name> ±HH:MM`` for an aware datetime, e.g. ``IST +05:30``."""
    offset = moment.utcoffset() or datetime.timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    name = moment.tzname() or "UTC"
    return f"{name} {sign}{hours:02d}:{minutes:02d}"


def get_current_time_and_region() -> dict:
    """
    Return the current local date/time and a region string.

    The result looks like:
        {
            "year": 2025,
            "month": 8,
            "day": 6,
            "hour": 14,
            "minute": 23,
            "second": 47,
            "region": "+05 +05:00"
        }
    """
    # Aware local time: its offset and zone name both follow DST.
    now = datetime.datetime.now().astimezone()
    region_str = format_region(now)

    return {
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "second": now.second,
        "region": region_str
    }