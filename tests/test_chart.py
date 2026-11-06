from collections import defaultdict
from datetime import date, datetime, time, timedelta

from noggin_compass.chart import day_label, render_chart
from noggin_compass.head import Side
from noggin_compass.i18n import set_language
from noggin_compass.report import DaySummary
from noggin_compass.sleep import SleepPeriod
from noggin_compass.tracker import Segment

T0 = datetime(2026, 11, 4, 21, 0)


def day(d: date, left_h: float, right_h: float) -> DaySummary:
    summary = DaySummary(d)
    summary.totals = defaultdict(float, {Side.LEFT: left_h * 3600, Side.RIGHT: right_h * 3600})
    return summary


def test_no_data_no_chart():
    assert render_chart(None, [], [], "title", time(12)) is None
    assert render_chart(DaySummary(date(2026, 11, 4)), [], [], "title", time(12)) is None


def test_chart_of_one_and_several_days_in_both_languages():
    recent = DaySummary(date(2026, 11, 4))
    recent.segments = [
        Segment(Side.LEFT, T0, T0 + timedelta(hours=1)),
        Segment(Side.CENTER, T0 + timedelta(hours=1), T0 + timedelta(hours=1, minutes=10)),
        Segment(Side.RIGHT, T0 + timedelta(hours=1, minutes=10), T0 + timedelta(hours=3)),
        Segment(Side.UNKNOWN, T0 + timedelta(hours=3), T0 + timedelta(hours=3, minutes=5)),
    ]
    for seg in recent.segments:
        recent.totals[seg.side] += seg.seconds
    # A sleep that began before the chart starts is cut at its left edge.
    sleep = [SleepPeriod(T0 - timedelta(hours=2), T0 + timedelta(hours=3)),
             SleepPeriod.nap(T0 + timedelta(hours=2), T0 + timedelta(hours=4))]
    for language in ("en", "ru"):
        set_language(language)
        one = render_chart(recent, sleep, [recent], "title", time(12))
        several = render_chart(recent, sleep, [day(date(2026, 11, 2), 3, 4), day(date(2026, 11, 3), 2, 5), recent], "title", time(12))
        assert one[:4] == several[:4] == b"\x89PNG"
        assert len(several) > len(one)


def test_day_label_names_the_night():
    assert day_label(date(2026, 11, 4), time(12)) == "04-05.11"
    assert day_label(date(2026, 10, 31), time(12)) == "31.10-01.11"
    assert day_label(date(2026, 11, 4), time(0)) == "04.11"
