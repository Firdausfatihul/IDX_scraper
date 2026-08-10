from idx_digest.timeutils import parse_boundary, parse_idx_datetime


def test_date_only_end_includes_full_day():
    parsed = parse_boundary("2026-08-05", "Asia/Jakarta", is_end=True)
    assert parsed.hour == 23 and parsed.minute == 59


def test_idx_naive_time_gets_jakarta_timezone():
    parsed = parse_idx_datetime("2026-08-05T23:53:32", "Asia/Jakarta")
    assert parsed.utcoffset().total_seconds() == 7 * 3600
