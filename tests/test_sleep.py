from datetime import date, datetime, time, timedelta

import pytest

from noggin_compass.config import SleepConfig
from noggin_compass.head import Side
from noggin_compass.sleep import Minute, classify_minutes, find_sleep, render_sleep, sleep_days, sleep_state

T0 = datetime(2026, 11, 4, 21, 0)
CFG = SleepConfig()
STILL, MOVING = 0.0, 0.2


def frames(pattern: str, start: datetime = T0) -> list[tuple[datetime, Side, float | None]]:
    """One character per minute, one frame per second: s still, m restless (every frame moves),
    t a still minute with a few twitches, o not in the crib, u in the crib but unseen (no box, so no
    movement value), _ no data."""
    samples = []
    for i, kind in enumerate(pattern):
        minute = start + timedelta(minutes=i)
        for sec in range(60):
            ts = minute + timedelta(seconds=sec)
            if kind == "s":
                samples.append((ts, Side.RIGHT, STILL))
            elif kind == "m":
                samples.append((ts, Side.UNKNOWN, MOVING))
            elif kind == "t":
                samples.append((ts, Side.RIGHT, MOVING if sec < 3 else STILL))
            elif kind == "o":
                samples.append((ts, Side.ABSENT, None))
            elif kind == "u":
                samples.append((ts, Side.UNKNOWN, None))
    return samples


def minutes_after(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def test_minutes_are_classified():
    kinds = classify_minutes(frames("smto"), CFG)
    assert list(kinds.values()) == [Minute.STILL, Minute.RESTLESS, Minute.STILL, Minute.OUT]


def test_minute_without_movement_data_is_missing():
    samples = [(T0 + timedelta(seconds=s), Side.RIGHT, None) for s in range(60)]
    assert classify_minutes(samples, CFG) == {T0: Minute.MISSING}


def test_min_frames_sets_how_many_measurements_a_minute_needs():
    # A frame every 20 seconds: three measurements a minute.
    samples = [(T0 + timedelta(seconds=s), Side.RIGHT, STILL) for s in (0, 20, 40)]
    assert classify_minutes(samples, CFG) == {T0: Minute.MISSING}
    assert classify_minutes(samples, SleepConfig(min_frames=3)) == {T0: Minute.STILL}


def test_falls_asleep_after_still_minutes_and_dates_it_back():
    periods = find_sleep(frames("mmm" + "s" * 30 + "m" * 7), CFG)
    assert len(periods) == 1
    assert periods[0].start == minutes_after(3)
    assert periods[0].end == minutes_after(33)
    assert not periods[0].ongoing


def test_short_calm_is_not_sleep():
    assert find_sleep(frames("mm" + "s" * 7 + "mm"), CFG) == []


def test_waving_arms_while_falling_asleep():
    # Eyes closed, but the arms keep moving for a minute now and then: never 8 still in a row.
    (period,) = find_sleep(frames("mmm" + "ssssmsssm" + "s" * 20), CFG)
    assert period.start == minutes_after(3)


def test_short_gaps_end_sleep_by_default():
    # Joining short gaps made up sleep in an empty crib, so by default any gap ends the sleep.
    periods = find_sleep(frames("s" * 12 + "o" + "s" * 10), CFG)
    assert [(p.start, p.end) for p in periods] == [(T0, minutes_after(12)), (minutes_after(13), minutes_after(23))]


def test_baby_lost_by_the_network_for_a_minute_or_two_sleeps_on():
    # In daylight a still baby in a white sleep sack is not found for a minute or two.
    cfg = SleepConfig(out_minutes=3)
    (period,) = find_sleep(frames("s" * 12 + "o" + "ss" + "oo" + "s" * 10), cfg)
    assert (period.start, period.end) == (T0, minutes_after(27))
    # Three minutes or more is being taken out of the crib.
    periods = find_sleep(frames("s" * 12 + "ooo" + "s" * 10), cfg)
    assert [(p.start, p.end) for p in periods] == [(T0, minutes_after(12)), (minutes_after(15), minutes_after(25))]


def test_twitches_and_single_restless_minutes_do_not_wake():
    periods = find_sleep(frames("s" * 20 + "tmtsmss" + "s" * 20), CFG)
    assert len(periods) == 1
    assert periods[0].seconds == pytest.approx(47 * 60)


def test_head_turns_in_light_sleep_do_not_wake():
    # Toward morning, with eyes closed, a newborn turns its head every few minutes and a parent
    # fixes the blanket: bursts of up to 4 restless minutes between short calm spells.
    pattern = "s" * 15 + "mmmmsmssm" + "sssss" + "mmm" + "sssss" + "mmm" + "s" * 10
    (period,) = find_sleep(frames(pattern), CFG)
    assert period.seconds == len(pattern) * 60


def test_restless_for_long_wakes():
    periods = find_sleep(frames("s" * 20 + "msmmmmmm" + "s" * 5), CFG)
    assert [(p.start, p.end) for p in periods] == [(T0, minutes_after(20))]


def test_taken_out_of_crib_ends_sleep():
    periods = find_sleep(frames("s" * 20 + "ooo" + "s" * 15), CFG)
    assert [(p.start, p.end) for p in periods] == [
        (T0, minutes_after(20)),
        (minutes_after(23), minutes_after(38)),
    ]


def test_long_gap_ends_sleep_short_one_does_not():
    # A restart of the program in the middle of the night is one sleep, not two.
    (period,) = find_sleep(frames("s" * 20 + "_" * 12 + "s" * 20), CFG)
    assert period.seconds == 52 * 60
    periods = find_sleep(frames("s" * 20 + "_" * 40 + "s" * 20), CFG)
    assert [p.seconds for p in periods] == [20 * 60, 20 * 60]


def test_waking_during_a_gap_shows_after_it():
    periods = find_sleep(frames("s" * 20 + "_" * 10 + "m" * 7 + "s" * 3), CFG)
    assert [(p.start, p.end) for p in periods] == [(T0, minutes_after(30))]


def test_sleep_still_going_is_ongoing():
    samples = frames("mm" + "s" * 15)
    periods = find_sleep(samples, CFG)
    assert periods[-1].ongoing
    asleep, period = sleep_state(samples, CFG, samples[-1][0])
    assert asleep and period.start == minutes_after(2)


def test_state_unknown_without_recent_movement_data():
    samples = frames("s" * 15)
    assert sleep_state(samples, CFG, samples[-1][0] + timedelta(hours=1)) == (None, None)
    samples = frames("s" * 15 + "m" * 7)
    assert sleep_state(samples, CFG, samples[-1][0]) == (False, None)


def test_a_sleep_across_midnight_gives_each_day_its_hours_and_one_falling_asleep():
    # Asleep from about 23:00 on the 5th to 01:00 on the 6th.
    start = datetime(2026, 11, 5, 23, 0)
    first, second = sleep_days(frames("s" * 120, start), CFG, time(0, 0))
    assert (first.day.day, second.day.day) == (5, 6)
    assert second.total == 3600 and second.count == 0  # the morning hours
    assert first.count == 1 and first.total == first.periods[0].seconds  # the falling asleep
    assert first.longest == second.longest == 7200  # the longest is the whole night in both
    assert first.periods[0].goes_on and second.periods[0].continued
    lines = render_sleep([first, second], time(0, 0), detail_days=2)
    assert "(goes on into the next day)" in lines and "00:00-01:00  1 h 00 min (from the day before)" in lines


def test_newborn_twitching_in_sleep_stays_asleep():
    # A minute with a fifth of its frames moving (twitches, a hand fixing the pacifier) is still.
    samples = frames("s" * 12)
    extra = frames("s" * 3, T0 + timedelta(minutes=12))
    for i in range(12):
        ts = extra[i][0]
        extra[i] = (ts, Side.RIGHT, MOVING)
    periods = find_sleep(samples + extra + frames("s" * 10, T0 + timedelta(minutes=15)), CFG)
    assert len(periods) == 1 and periods[0].seconds == 25 * 60


def test_no_sleep_message_depends_on_movement_data():
    from noggin_compass.sleep import no_sleep_text

    assert "yet" in no_sleep_text(frames("mm"))
    assert "older versions" in no_sleep_text([(T0, Side.RIGHT, None)])


def test_naps_and_crib_sleep_stay_apart_but_overlaps_count_once():
    from noggin_compass.sleep import SleepPeriod, asleep_seconds, shared, with_naps

    crib = [SleepPeriod(T0, minutes_after(60)), SleepPeriod(minutes_after(200), minutes_after(260), ongoing=True)]
    naps = [
        SleepPeriod.nap(minutes_after(-120), minutes_after(-60)),  # in arms, before
        SleepPeriod.nap(minutes_after(40), minutes_after(90)),  # overlaps the crib sleep
        SleepPeriod.nap(minutes_after(100), minutes_after(130)),
        SleepPeriod.nap(minutes_after(110), minutes_after(120)),  # entered twice
        SleepPeriod.nap(minutes_after(130), minutes_after(140)),  # goes on from the one before
    ]
    merged = with_naps(crib, naps)
    assert [(p.start, p.end, p.manual, p.ongoing) for p in merged] == [
        (minutes_after(-120), minutes_after(-60), True, False),
        (T0, minutes_after(60), False, False),
        (minutes_after(40), minutes_after(90), True, False),
        (minutes_after(100), minutes_after(140), True, False),
        (minutes_after(200), minutes_after(260), False, True),
    ]
    assert shared(merged) == [(minutes_after(40), minutes_after(60))]
    assert asleep_seconds(merged) == (60 + 90 + 40 + 60) * 60


def test_naps_count_in_sleep_days_and_are_marked():
    from noggin_compass.sleep import SleepPeriod, render_sleep

    nap = SleepPeriod.nap(minutes_after(-120), minutes_after(-60))
    days = sleep_days(frames("s" * 30), CFG, time(12, 0), [nap])
    assert [(d.total, len(d.periods)) for d in days] == [(90 * 60, 2)]
    assert "19:00-20:00  1 h 00 min (outside the crib)" in render_sleep(days, time(12, 0))


def test_state_unknown_while_the_baby_in_the_crib_is_unseen():
    # Asleep, then the pose model loses the baby (a white sleep sack at night) while the presence
    # check still sees it: no movement data, so the bot must not say the baby is awake.
    samples = frames("s" * 15 + "u" * 4)
    assert sleep_state(samples, CFG, samples[-1][0]) == (None, None)
    samples = frames("s" * 15 + "u" * 1)
    asleep, _ = sleep_state(samples, CFG, samples[-1][0])
    assert asleep


def test_overlap_is_marked_on_both_sleeps():
    from noggin_compass.sleep import SleepPeriod, period_lines, with_naps

    merged = with_naps([SleepPeriod(T0, minutes_after(60))], [SleepPeriod.nap(minutes_after(40), minutes_after(90))])
    assert period_lines(merged, now=minutes_after(600)) == [
        "21:00-22:00  1 h 00 min (overlaps another sleep: 21:40-22:00)",
        "21:40-22:30  50 min (outside the crib) (overlaps another sleep: 21:40-22:00)",
    ]


def test_a_nap_right_before_a_crib_sleep_is_another_sleep():
    # Taken from the pram and put into the crib: the baby woke up and fell asleep again.
    from noggin_compass.sleep import SleepPeriod

    pram = SleepPeriod.nap(minutes_after(-30), T0)
    (day,) = sleep_days(frames("s" * 30), CFG, time(0, 0), [pram])
    assert (day.count, day.total) == (2, 60 * 60)
