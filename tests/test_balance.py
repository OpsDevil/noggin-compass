from datetime import datetime, timedelta

from noggin_compass.balance import BalanceMonitor
from noggin_compass.head import Side
from noggin_compass.tracker import Segment

T0 = datetime(2026, 11, 4, 21, 0)
L, R, C, A = Side.LEFT, Side.RIGHT, Side.CENTER, Side.ABSENT
H = timedelta(hours=1)
M = timedelta(minutes=1)


def monitor(**kw):
    defaults = dict(max_same_side=3 * H, max_imbalance=3 * H, window=12 * H, min_turn=15 * M, repeat=15 * M)
    return BalanceMonitor(**{**defaults, **kw})


def run(mon, *plan, step=M):
    """Play a plan [(side, duration), ...] minute by minute and return the alerts as
    [(time since T0, alert), ...]."""
    alerts, t = [], T0
    for side, length in plan:
        start = t
        while t < start + length:
            t += step
            alert = mon.check(t, Segment(side, start, t))
            if alert:
                alerts.append((t - T0, alert))
        mon.add([Segment(side, start, t)])
    return alerts


def test_three_hours_on_one_side_is_fine_then_alert():
    alerts = run(monitor(), (R, 3 * H + 20 * M))
    first_at, first = alerts[0]
    assert (first.reason, first.side, first.turn_to) == ("stint", R, L)
    assert [at for at, _ in alerts] == [3 * H, 3 * H + 15 * M]  # then a repeat 15 minutes later


def test_alternating_three_hours_never_alerts():
    assert run(monitor(), (R, 3 * H - M), (L, 3 * H - M), (R, 3 * H - M), (L, 3 * H - M)) == []


def test_short_turns_do_not_rebalance():
    # 2 h right, 30 min left, right again: the 3 h imbalance is reached 1.5 h into the second stint.
    alerts = run(monitor(), (R, 2 * H), (L, 30 * M), (R, 2 * H))
    at, alert = alerts[0]
    assert at == 2 * H + 30 * M + 90 * M
    assert (alert.reason, alert.turn_to) == ("imbalance", L)
    assert alert.totals[R] == 3.5 * 3600 and alert.totals[L] == 0.5 * 3600


def test_brief_glance_does_not_reset_the_stint_but_a_real_turn_does():
    alerts = run(monitor(max_imbalance=timedelta(0)), (R, 2 * H), (L, 5 * M), (R, 61 * M))
    assert alerts and alerts[0][1].reason == "stint"
    assert run(monitor(max_imbalance=timedelta(0)), (R, 2 * H), (L, 20 * M), (R, 61 * M)) == []


def test_center_does_not_count_and_does_not_reset():
    alerts = run(monitor(max_imbalance=timedelta(0)), (R, 2 * H), (C, H), (R, H))
    assert alerts and alerts[0][0] == 4 * H


def test_new_sleep_after_long_absence_resets_the_stint():
    assert run(monitor(max_imbalance=timedelta(0)), (R, 2 * H), (A, 40 * M), (R, 2 * H)) == []
    assert run(monitor(max_imbalance=timedelta(0)), (R, 2 * H), (A, 10 * M), (R, 2 * H)) != []


def test_imbalance_counts_only_the_window():
    quiet = monitor(max_same_side=timedelta(0), window=4 * H)
    assert run(quiet, (R, 2 * H + 50 * M), (C, 5 * H), (R, H)) == []
    loud = monitor(max_same_side=timedelta(0), window=4 * H)
    alerts = run(loud, (R, 2 * H + 50 * M), (C, 10 * M), (R, 20 * M))
    assert [alert.reason for _, alert in alerts] == ["imbalance"]


def test_disabled_by_default_values():
    mon = monitor(max_same_side=timedelta(0), max_imbalance=timedelta(0))
    assert not mon.enabled
    assert run(mon, (R, 10 * H)) == []


def test_long_gap_without_data_starts_a_new_stint():
    mon = monitor(max_imbalance=timedelta(0))
    mon.add([Segment(R, T0, T0 + 2 * H)])
    after_short_gap = T0 + 2 * H + 10 * M
    assert mon.stint(after_short_gap + H, Segment(R, after_short_gap, after_short_gap + H)) == 3 * 3600
    after_long_gap = T0 + 2 * H + 40 * M
    assert mon.stint(after_long_gap + H, Segment(R, after_long_gap, after_long_gap + H)) == 3600
