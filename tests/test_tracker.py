from datetime import datetime, timedelta

from noggin_compass.head import Side
from noggin_compass.tracker import build_segments

T0 = datetime(2026, 10, 29, 22, 0, 0)
L, R, U = Side.LEFT, Side.RIGHT, Side.UNKNOWN


def series(*runs: tuple[Side, int], start: datetime = T0) -> list[tuple[datetime, Side]]:
    """Samples at 1 frame per second from consecutive runs of (side, seconds)."""
    out, t = [], start
    for side, seconds in runs:
        for _ in range(seconds):
            out.append((t, side))
            t += timedelta(seconds=1)
    return out


def summary(segments):
    return [(s.side, s.seconds) for s in segments]


def test_steady_side_is_one_segment():
    assert summary(build_segments(series((L, 61)), 10, 60)) == [(L, 60)]


def test_short_flicker_is_ignored():
    segments = build_segments(series((L, 30), (R, 3), (L, 30)), 10, 60)
    assert summary(segments) == [(L, 62)]


def test_confirmed_switch_starts_at_first_new_frame():
    segments = build_segments(series((L, 30), (R, 31)), 10, 60)
    assert summary(segments) == [(L, 30), (R, 30)]


def test_gap_is_not_counted():
    samples = series((L, 31)) + series((L, 31), start=T0 + timedelta(minutes=10))
    assert summary(build_segments(samples, 10, 60)) == [(L, 30), (L, 30)]


def test_unknown_is_tracked_separately():
    segments = build_segments(series((L, 30), (U, 30), (L, 31)), 10, 60)
    assert summary(segments) == [(L, 30), (U, 30), (L, 30)]
