from app.utils.time_utils import beijing_iso_time, format_beijing_time, parse_datetime


def test_utc_iso_time_is_rendered_as_beijing_24_hour_time():
    value = "2026-07-22T03:36:02.824341+00:00"

    assert format_beijing_time(value) == "2026-07-22 11:36:02"
    assert beijing_iso_time(value) == "2026-07-22T11:36:02+08:00"


def test_naive_database_time_is_treated_as_utc():
    parsed = parse_datetime("2026-07-22T16:05:06")

    assert parsed is not None
    assert parsed.isoformat(timespec="seconds") == "2026-07-23T00:05:06+08:00"


def test_invalid_time_has_stable_placeholder():
    assert format_beijing_time("not-a-time") == "-"
    assert beijing_iso_time("not-a-time") is None
