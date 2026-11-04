from datetime import date, datetime, time, timedelta

from noggin_compass.head import Side
from noggin_compass.report import build_report, reclassify, render, split_by_day, summarize
from noggin_compass.tracker import Segment

NOON = time(12, 0)


def seg(side, start, minutes):
    return Segment(side, start, start + timedelta(minutes=minutes))


def test_night_belongs_to_one_day_and_noon_splits():
    evening = datetime(2026, 10, 29, 21, 0)
    late_morning = datetime(2026, 10, 30, 11, 30)
    days = split_by_day([seg(Side.LEFT, evening, 60), seg(Side.RIGHT, late_morning, 60)], NOON)
    assert sorted(days) == [date(2026, 10, 29), date(2026, 10, 30)]
    assert [s.seconds for s in days[date(2026, 10, 29)]] == [3600, 1800]
    assert [s.seconds for s in days[date(2026, 10, 30)]] == [1800]


def test_turns_skip_unknown_and_absent():
    t = datetime(2026, 10, 29, 22, 0)
    sides = [Side.LEFT, Side.CENTER, Side.LEFT, Side.UNKNOWN, Side.LEFT, Side.ABSENT, Side.RIGHT]
    segments = [seg(side, t + timedelta(minutes=10 * i), 10) for i, side in enumerate(sides)]
    summary = summarize(date(2026, 10, 29), segments)
    assert summary.turns == 3
    assert summary.totals[Side.LEFT] == 1800
    assert summary.share(Side.LEFT) == 0.6


def test_threshold_change_applies_to_old_samples():
    t = datetime(2026, 10, 29, 22, 0)
    samples = [(t, Side.LEFT, 0.4), (t, Side.CENTER, 0.1), (t, Side.UNKNOWN, None)]
    assert [s for _, s in reclassify(samples, 0.5)] == [Side.CENTER, Side.CENTER, Side.UNKNOWN]


def test_render_smoke():
    t = datetime(2026, 10, 29, 22, 0)
    samples = [(t + timedelta(seconds=i), Side.LEFT, 0.6) for i in range(600)]
    samples += [(t + timedelta(seconds=600 + i), Side.RIGHT, -0.6) for i in range(300)]
    summaries = build_report(samples, center_threshold=0.35, confirm_seconds=10, gap_seconds=60, day_start=NOON)
    text = render(summaries, NOON)
    assert "29.10" in text
    assert "10 min" in text and "5 min" in text
    assert "22:00 │" in text
