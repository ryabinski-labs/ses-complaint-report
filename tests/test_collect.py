import datetime as dt

from sesreport.collect import _day, _iso


def test_day_uses_wall_clock_not_utc():
    # CloudWatch publishes these buckets in local time; a 20:00-04:00 bucket is
    # still that same day, not the next day in UTC.
    tz = dt.timezone(dt.timedelta(hours=-4))
    d = dt.datetime(2026, 9, 15, 20, 0, tzinfo=tz)
    assert _day(d) == "2026-09-15"


def test_day_of_plain_date():
    assert _day("2026-05-18") == "2026-05-18"


def test_iso_normalises_datetime_to_utc():
    tz = dt.timezone(dt.timedelta(hours=-4))
    d = dt.datetime(2026, 5, 19, 3, 36, 50, tzinfo=tz)
    assert _iso(d).startswith("2026-05-19T07:36:50")


def test_iso_passes_through_strings():
    assert _iso("2026-05-19T07:36:50Z") == "2026-05-19T07:36:50Z"
