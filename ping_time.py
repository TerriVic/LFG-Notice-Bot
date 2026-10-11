"""Timestamp parsing for the optional /ping_game scheduling argument.

Discord does not expose its native @time picker to app-command parameters.
Never guess a sender's timezone: relative expressions are timezone-independent,
and absolute date/time strings must contain an explicit UTC offset.
"""

import re
from datetime import datetime, time as clock_time, timedelta, timezone

TIME_STYLES = {
    "R": "Relative (in 2 hours)",
    "t": "Short time (20:00)",
    "T": "Medium time (20:00:00)",
    "d": "Short date (12/10/2026)",
    "D": "Long date (12 October 2026)",
    "f": "Date and time",
    "F": "Full date and time",
    "s": "Short date and time",
    "S": "Short date and time, seconds",
}

TIME_PRESETS = (
    ("In 15 minutes", "15m"),
    ("In 30 minutes", "30m"),
    ("In 45 minutes", "45m"),
    ("In 1 hour", "1h"),
    ("In 1 hour 30 minutes", "1h 30m"),
    ("In 2 hours", "2h"),
    ("In 3 hours", "3h"),
    ("In 4 hours", "4h"),
    ("In 6 hours", "6h"),
    ("In 12 hours", "12h"),
    ("In 1 day", "1d"),
    ("In 2 days", "2d"),
    ("In 1 week", "1w"),
)

_TIMESTAMP = re.compile(r"<t:(\d{1,12})(?::([tTdDfFsSR]))?>")
_UNIT = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*"
    r"(weeks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    re.IGNORECASE,
)
_LOCAL_DAY = re.compile(
    r"(today|tomorrow)\s+(\d{1,2}:\d{2})\s*(Z|[+-]\d{2}:\d{2})",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    'w': 604800, 'd': 86400, 'h': 3600, 'm': 60, 's': 1,
}


def _unit_seconds(unit: str) -> int:
    lower = unit.lower()
    if lower.startswith('w'):
        return _UNIT_SECONDS['w']
    if lower.startswith('d'):
        return _UNIT_SECONDS['d']
    if lower.startswith('h'):
        return _UNIT_SECONDS['h']
    if lower.startswith('m'):
        return _UNIT_SECONDS['m']
    return _UNIT_SECONDS['s']


def _offset_zone(text: str):
    if text.upper() == 'Z':
        return timezone.utc
    sign = -1 if text[0] == '-' else 1
    hours, minutes = map(int, text[1:].split(':'))
    if hours > 23 or minutes > 59:
        raise ValueError('Invalid UTC offset.')
    # datetime.timezone supports UTC offsets strictly between -24h and +24h.
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_ping_time(value: str, *, now: datetime | None = None) -> tuple[int, str | None]:
    """Return (Unix seconds, pasted Discord timestamp style or None).

    Accept duration phrases (30m, in 2 hours, 1h30m), Discord timestamps,
    integer Unix seconds, timezone-aware ISO-8601 timestamps, and
    'tomorrow 20:00+03:00'. Bare local times are deliberately not accepted.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Enter a start time.')
    text = value.strip()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('The reference clock must have a timezone.')

    pasted_style = None
    match = _TIMESTAMP.fullmatch(text)
    if match:
        timestamp = int(match.group(1))
        pasted_style = match.group(2)
    elif re.fullmatch(r"\d{9,12}", text):
        timestamp = int(text)
    else:
        relative = re.sub(r"(?i)^in\s+", "", text)
        consumed = 0
        seconds = 0.0
        for part in _UNIT.finditer(relative):
            if part.start() != consumed:
                break
            seconds += float(part.group(1)) * _unit_seconds(part.group(2))
            consumed = part.end()
        if consumed and consumed == len(relative):
            if not (0 < seconds <= 366 * 86400):
                raise ValueError('Relative times must be between 1 second and 366 days.')
            timestamp = int(now.timestamp() + seconds)
        else:
            day_match = _LOCAL_DAY.fullmatch(text)
            if day_match:
                day, hhmm, offset = day_match.groups()
                zone = _offset_zone(offset)
                target_day = now.astimezone(zone).date()
                if day.lower() == 'tomorrow':
                    target_day += timedelta(days=1)
                hours, minutes = map(int, hhmm.split(':'))
                try:
                    target = datetime.combine(target_day, clock_time(hours, minutes), tzinfo=zone)
                except ValueError as err:
                    raise ValueError('Invalid hour or minute.') from err
                timestamp = int(target.timestamp())
            else:
                # Only accept absolute dates with explicit offsets; server
                # timezone and the author's timezone need not be the same.
                try:
                    absolute = datetime.fromisoformat(text.replace('Z', '+00:00'))
                except ValueError as err:
                    raise ValueError(
                        'Use 30m, 2h, a Discord <t:...> timestamp, or '
                        '2026-10-12T20:00+03:00 (with timezone).'
                    ) from err
                if absolute.tzinfo is None or absolute.utcoffset() is None:
                    raise ValueError('Include a timezone offset, e.g. +03:00 or Z (UTC).')
                timestamp = int(absolute.timestamp())

    if timestamp <= int(now.timestamp()):
        raise ValueError('The start time must be in the future.')
    # Limit to valid dates accepted by Python datetime and prevent stray huge ints.
    try:
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, OverflowError, OSError) as err:
        raise ValueError('The time is outside the supported date range.') from err
    return timestamp, pasted_style
