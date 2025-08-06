import datetime
import time

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
            "region": "ЕКБ +5"
        }
    """
    # Current local datetime
    now = datetime.datetime.now()

    # UTC offset in hours (e.g., +5 or -3)
    utc_offset_td = now.astimezone().utcoffset()
    if utc_offset_td is None:
        # Fallback to 0 if timezone info unavailable
        utc_offset_hours = 0
    else:
        utc_offset_hours = int(utc_offset_td.total_seconds() // 3600)

    # Time‑zone abbreviation (e.g., 'EST', 'EDT', 'ЕКБ')
    tz_abbr = time.tzname[0]   # first entry is the abbreviation for DST if applicable

    region_str = f"{tz_abbr} {utc_offset_hours:+d}"

    return {
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "second": now.second,
        "region": region_str
    }