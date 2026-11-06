"""Sleep from presence in the crib and movement, minute by minute, the way sleep actigraphy works:
a baby lying still for a while is asleep, a restless one is awake.

It cannot tell sleep from lying still with open eyes, and it only sees the crib. Naps in arms or
in a pram are entered by the parents in Telegram and added with with_naps."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from .config import SleepConfig
from .head import Side
from .i18n import t
from .report import day_bounds, day_of, fmt_duration

MINUTE = timedelta(minutes=1)
# A minute needs at least this many movement measurements, otherwise it counts as missing.
MIN_FRAMES = 5
# A pause in the data shorter than this (the program restarted, the camera reconnected) does not
# end a sleep: if the baby woke up meanwhile, the restless minutes after it show that. A longer
# one ends the sleep where the data stopped: we don't know what happened.
MAX_GAP = timedelta(minutes=30)
# Movement data this recent counts as now: older, and nothing is said about whether the baby is
# asleep right now, nor is a sleep shown as going on.
FRESH = timedelta(minutes=2)


class Minute(StrEnum):
    STILL = "still"
    RESTLESS = "restless"
    OUT = "out"  # the baby is not in the crib
    MISSING = "missing"


@dataclass
class SleepPeriod:
    start: datetime
    end: datetime
    # True if the data ended while the baby was still asleep.
    ongoing: bool = False
    # Entered by the parents, not seen by the camera.
    manual: bool = False
    # A piece of a sleep cut at the start of a day: it began the day before.
    continued: bool = False
    # A piece of a sleep cut at the end of a day: it goes on into the next one.
    goes_on: bool = False

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def classify_minutes(
    samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig
) -> dict[datetime, Minute]:
    buckets: dict[datetime, list[tuple[Side, float | None]]] = defaultdict(list)
    for ts, side, motion in samples:
        buckets[ts.replace(second=0, microsecond=0)].append((side, motion))
    minutes = {}
    for minute, rows in buckets.items():
        if 2 * sum(side == Side.ABSENT for side, _ in rows) > len(rows):
            minutes[minute] = Minute.OUT
            continue
        motions = [m for _, m in rows if m is not None]
        if len(motions) < MIN_FRAMES:
            minutes[minute] = Minute.MISSING
            continue
        moving = sum(m >= cfg.frame_motion for m in motions) / len(motions)
        minutes[minute] = Minute.RESTLESS if moving >= cfg.restless_share else Minute.STILL
    return minutes


def _short_absences_still(minutes: dict[datetime, Minute], out_minutes: int) -> dict[datetime, Minute]:
    """Out-of-crib minutes in a run shorter than out_minutes, with the baby seen again after it,
    become still. In daylight the network loses a still baby in a sleep sack for a minute or two,
    while a baby taken out stays out much longer."""
    order = [m for m in sorted(minutes) if minutes[m] != Minute.MISSING]
    result = dict(minutes)
    i = 0
    while i < len(order):
        if minutes[order[i]] != Minute.OUT:
            i += 1
            continue
        j = i
        while j + 1 < len(order) and minutes[order[j + 1]] == Minute.OUT:
            j += 1
        seen_again = j + 1 < len(order)
        if seen_again and order[j] - order[i] < (out_minutes - 1) * MINUTE:
            for m in order[i : j + 1]:
                result[m] = Minute.STILL
        i = j + 1
    return result


def find_sleep(samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig) -> list[SleepPeriod]:
    """Asleep after fall_asleep_minutes still minutes within fall_asleep_window_minutes, dated to
    the first of them. Awake after wake_minutes restless minutes within wake_window_minutes, dated
    to the first restless one, or when the baby has been out of the crib for out_minutes. Twitches,
    head turns and waving arms during sleep do not wake it."""
    minutes = _short_absences_still(classify_minutes(samples, cfg), cfg.out_minutes)
    periods: list[SleepPeriod] = []
    asleep: datetime | None = None
    recent: deque[tuple[datetime, bool]] = deque()  # (minute, still) within the fall-asleep window
    restless: deque[datetime] = deque()
    last: datetime | None = None  # last minute with data

    def wake(at: datetime, ongoing: bool = False) -> None:
        nonlocal asleep
        if asleep is not None and at > asleep:
            periods.append(SleepPeriod(asleep, at, ongoing))
        asleep = None
        restless.clear()
        # The still minutes before waking belong to the sleep that just ended: counted again,
        # they could date the next sleep before this wake and merge the two into one.
        recent.clear()

    for minute in sorted(minutes):
        kind = minutes[minute]
        if kind == Minute.MISSING:
            continue
        if last is not None and minute - last > MAX_GAP:
            wake(last + MINUTE)
            recent.clear()
        last = minute
        if kind == Minute.OUT:
            wake(minute)
            recent.clear()
            continue
        recent.append((minute, kind == Minute.STILL))
        while recent[0][0] <= minute - cfg.fall_asleep_window_minutes * MINUTE:
            recent.popleft()
        if asleep is None:
            if kind == Minute.STILL and sum(still for _, still in recent) >= cfg.fall_asleep_minutes:
                asleep = next(m for m, still in recent if still)
        elif kind == Minute.RESTLESS:
            restless.append(minute)
            while restless[0] <= minute - cfg.wake_window_minutes * MINUTE:
                restless.popleft()
            if len(restless) >= cfg.wake_minutes:
                wake(restless[0])
    if last is not None:
        wake(last + MINUTE, ongoing=True)
    return periods


def split_by_day(periods: list[SleepPeriod], day_start: time) -> dict[date, list[SleepPeriod]]:
    """Each sleep cut at the day boundaries: a night from 23:00 to 07:00 is an hour in the first
    day and 7 hours in the second. The pieces say they were cut, so a day counts the time it holds
    and the falling asleep only where it happened."""
    days: dict[date, list[SleepPeriod]] = defaultdict(list)
    for period in periods:
        start = period.start
        while start < period.end:
            day = day_of(start, day_start)
            end = min(period.end, day_bounds(day, day_start)[1])
            days[day].append(SleepPeriod(start, end, period.ongoing and end == period.end, period.manual,
                                         continued=start > period.start, goes_on=end < period.end))
            start = end
    return dict(days)


@dataclass
class SleepDay:
    """A day's sleep: the pieces of the sleeps that fall into it (see split_by_day)."""

    day: date
    periods: list[SleepPeriod]

    @property
    def total(self) -> float:
        """Time asleep within the day."""
        return sum(p.seconds for p in self.periods)

    @property
    def count(self) -> int:
        """Times the baby fell asleep that day: a sleep from the night before is not one of them."""
        return sum(not p.continued for p in self.periods)

    @property
    def longest(self) -> float:
        """The longest stretch of sleep within the day."""
        return max((p.seconds for p in self.periods), default=0.0)


def within(periods: list[SleepPeriod], since: datetime) -> list[SleepPeriod]:
    """What of each period falls after `since`: for the totals and the longest of a window, so a
    sleep that began before it counts only its part inside."""
    return [SleepPeriod(max(p.start, since), p.end, p.ongoing, p.manual) for p in periods if p.end > since]


def with_naps(periods: list[SleepPeriod], naps: list[SleepPeriod]) -> list[SleepPeriod]:
    """Sleep the camera found plus the naps the parents entered, in time order. Overlapping ones
    become one sleep, so a nap entered for a sleep the camera also saw, or entered twice, is not
    counted twice."""
    merged: list[SleepPeriod] = []
    for p in sorted(list(periods) + list(naps), key=lambda p: p.start):
        last = merged[-1] if merged else None
        if last is not None and p.start <= last.end:
            ongoing = p.ongoing if p.end >= last.end else last.ongoing
            merged[-1] = SleepPeriod(last.start, max(last.end, p.end), ongoing, last.manual and p.manual)
        else:
            merged.append(p)
    return merged


def sleep_days(
    samples: list[tuple[datetime, Side, float | None]],
    cfg: SleepConfig,
    day_start: time,
    naps: list[SleepPeriod] = (),
) -> list[SleepDay]:
    days = split_by_day(with_naps(find_sleep(samples, cfg), naps), day_start)
    return [SleepDay(day, periods) for day, periods in sorted(days.items())]


def period_lines(periods: list[SleepPeriod], now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    lines = []
    for p in periods:
        line = t(
            # A sleep cut off by the end of old data is not "now".
            "sleep.ongoing" if p.ongoing and now - p.end <= FRESH else "sleep.period",
            start=f"{p.start:%H:%M}",
            end=f"{p.end:%H:%M}",
            duration=fmt_duration(p.seconds),
        )
        if p.manual:
            line = t("sleep.manual", line=line)
        if p.continued:
            line = t("sleep.continued", line=line)
        if p.goes_on:
            line = t("sleep.goes_on", line=line)
        lines.append(line)
    return lines


def no_sleep_text(samples: list[tuple[datetime, Side, float | None]]) -> str:
    """Why there is no sleep: none yet, or no movement data to tell it from."""
    has_motion = any(m is not None for _, _, m in samples)
    return t("sleep.none_yet") if has_motion else t("sleep.none")


def render_sleep(
    days: list[SleepDay], day_start: time, detail_days: int = 1, samples: list | None = None
) -> str:
    if not days:
        return no_sleep_text(samples or [])
    weekdays = t("weekdays").split(",")
    out = [t("sleep.title_midnight") if day_start == time(0, 0) else t("sleep.title", start=f"{day_start:%H:%M}")]
    for d in days:
        out.append(
            f"{d.day:%d.%m} ({weekdays[d.day.weekday()]})  "
            + t("sleep.day", total=fmt_duration(d.total), count=d.count, longest=fmt_duration(d.longest))
        )
    for d in days[-detail_days:] if detail_days else []:
        out += ["", t("sleep.day_title", date=f"{d.day:%d.%m.%Y}")]
        out += [f"  {line}" for line in period_lines(d.periods)]
    return "\n".join(out)


def sleep_state(
    samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig, now: datetime
) -> tuple[bool | None, SleepPeriod | None]:
    """(asleep, current sleep) at `now`; (None, None) when there is no recent movement data.
    Recent means a minute that could be classified: a baby in the crib whom the pose model lost
    gives a few movement values a minute at best, which says nothing about sleep."""
    known = [m for m, kind in classify_minutes(samples, cfg).items() if kind != Minute.MISSING]
    if not known or now - (max(known) + MINUTE) > FRESH:
        return None, None
    periods = find_sleep(samples, cfg)
    if periods and periods[-1].ongoing and now - periods[-1].end <= FRESH:
        return True, periods[-1]
    return False, None


# How far back sleep is looked for, to see a sleep that began before the period asked about.
SLEEP_LOOKBACK = timedelta(hours=12)


def sleep_line(samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig, now: datetime) -> str | None:
    """"Asleep for ..., since ..." or "Not asleep", as /now and the picture show it; None without
    recent movement data."""
    asleep, period = sleep_state(samples, cfg, now)
    if asleep:
        return t("bot.now_asleep", duration=fmt_duration(period.seconds), since=f"{period.start:%H:%M}")
    return None if asleep is None else t("bot.now_awake")
