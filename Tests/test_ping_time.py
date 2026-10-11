from datetime import datetime, timezone

import pytest

from ping_time import parse_ping_time

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize('raw,seconds', [
    ('30m', 1800),
    ('in 2 hours', 7200),
    ('1h30m', 5400),
    ('1h 30m', 5400),
    ('1.5h', 5400),
    ('tomorrow 15:30Z', 99000),
])
def test_relative_inputs(raw, seconds):
    assert parse_ping_time(raw, now=NOW) == (int(NOW.timestamp()) + seconds, None)


def test_discord_timestamp_preserves_style():
    ts = int(NOW.timestamp()) + 3600
    assert parse_ping_time(f'<t:{ts}:F>', now=NOW) == (ts, 'F')
    assert parse_ping_time(str(ts), now=NOW) == (ts, None)
    assert parse_ping_time(f'<t:{ts}>', now=NOW) == (ts, None)


def test_absolute_time_requires_timezone_and_honors_offset():
    assert parse_ping_time('2026-10-12T20:00+03:00', now=NOW)[0] == int(datetime(2026, 10, 12, 17, tzinfo=timezone.utc).timestamp())
    assert parse_ping_time('2026-10-12T17:00Z', now=NOW)[0] == int(datetime(2026, 10, 12, 17, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError, match='timezone offset'):
        parse_ping_time('2026-10-12 20:00', now=NOW)


@pytest.mark.parametrize('raw', ['tomorrow 20:00', '8pm', '1hour rubbish', '1h-30m', '2 hours 10x', '0m', 'in -2h', '2026-10-10T12:00Z', '<t:1234:R>'])
def test_invalid_times(raw):
    with pytest.raises(ValueError):
        parse_ping_time(raw, now=NOW)


@pytest.mark.parametrize('style', ['t', 'T', 'd', 'D', 'f', 'F', 's', 'S', 'R'])
def test_all_discord_timestamp_styles_remain_valid_inputs(style):
    ts = int(NOW.timestamp()) + 3600
    assert parse_ping_time(f'<t:{ts}:{style}>', now=NOW)[0] == ts
