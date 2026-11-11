"""
Deterministic Asia/Kolkata (IST) date resolution.
Strictly pure functions with no LLM or database dependencies.
"""

from datetime import datetime, date, timedelta
from typing import Tuple, Optional
import calendar
import pytz

IST_TIMEZONE = pytz.timezone("Asia/Kolkata")


def get_current_ist_datetime() -> datetime:
    """Return current datetime in Asia/Kolkata timezone."""
    return datetime.now(IST_TIMEZONE)


def get_current_ist_date() -> date:
    """Return current date in Asia/Kolkata timezone."""
    return get_current_ist_datetime().date()


def resolve_date_range(symbolic_range: Optional[str], reference_date: Optional[date] = None) -> Tuple[date, date]:
    """
    Resolve symbolic date ranges into exact IST [start_date, end_date] bounds (inclusive).
    Supports: 'today', 'this_week', 'last_month', 'last_90_days', 'all'.
    """
    ref_date = reference_date or get_current_ist_date()
    range_str = (symbolic_range or "all").strip().lower()

    if range_str == "today":
        return ref_date, ref_date

    elif range_str in ("this_week", "this week"):
        # Monday to Sunday of the current week
        start_date = ref_date - timedelta(days=ref_date.weekday())
        end_date = start_date + timedelta(days=6)
        return start_date, end_date

    elif range_str in ("last_month", "last month"):
        # 1st of previous month to the last day of previous month
        if ref_date.month == 1:
            prev_year = ref_date.year - 1
            prev_month = 12
        else:
            prev_year = ref_date.year
            prev_month = ref_date.month - 1

        _, last_day = calendar.monthrange(prev_year, prev_month)
        start_date = date(prev_year, prev_month, 1)
        end_date = date(prev_year, prev_month, last_day)
        return start_date, end_date

    elif range_str in ("last_90_days", "last 90 days"):
        return ref_date - timedelta(days=90), ref_date

    else:
        # Default 'all' or unconstrained: span back 1 year to today
        return ref_date - timedelta(days=365), ref_date
