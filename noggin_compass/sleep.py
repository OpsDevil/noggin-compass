"""Sleep from presence in the crib and movement, minute by minute, the way sleep actigraphy works:
a baby lying still for a while is asleep, a restless one is awake.

It cannot tell sleep from lying still with open eyes, and it only sees the crib: naps in arms or
in a stroller are not counted."""

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
# Missing data for longer than this (program stopped, camera off) ends a sleep: we don't know.
MAX_GAP = timedelta(minutes=5)


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


def find_sleep(samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig) -> list[SleepPeriod]:
    """Asleep after fall_asleep_minutes still minutes in a row, dated to the first of them.
    Awake after wake_minutes restless minutes within wake_window_minutes, dated to the first
    restless one, or as soon as the baby leaves the crib. Single twitches during sleep do not
    wake it."""
    minutes = classify_minutes(samples, cfg)
    periods: list[SleepPeriod] = []
    asleep: datetime | None = None
    still_since: datetime | None = None
    restless: deque[datetime] = deque()
    last: datetime | None = None  # last minute with data

    def wake(at: datetime, ongoing: bool = False) -> None:
        nonlocal asleep
        if asleep is not None and at > asleep:
            periods.append(SleepPeriod(asleep, at, ongoing))
        asleep = None
        restless.clear()

    for minute in sorted(minutes):
        kind = minutes[minute]
        if kind == Minute.MISSING:
            continue
        if last is not None and minute - last > MAX_GAP:
            wake(last + MINUTE)
            still_since = None
        last = minute
        if kind == Minute.OUT:
            wake(minute)
            still_since = None
        elif kind == Minute.STILL:
            still_since = still_since or minute
            if asleep is None and minute - still_since >= (cfg.fall_asleep_minutes - 1) * MINUTE:
                asleep = still_since
        else:
            still_since = None
            if asleep is not None:
                restless.append(minute)
                while restless[0] <= minute - cfg.wake_window_minutes * MINUTE:
                    restless.popleft()
                if len(restless) >= cfg.wake_minutes:
                    wake(restless[0])
    if last is not None:
        wake(last + MINUTE, ongoing=True)
    return periods


def split_by_day(periods: list[SleepPeriod], day_start: time) -> dict[date, list[SleepPeriod]]:
    days: dict[date, list[SleepPeriod]] = defaultdict(list)
    for period in periods:
        start = period.start
        while start < period.end:
            day = day_of(start, day_start)
            end = min(period.end, day_bounds(day, day_start)[1])
            days[day].append(SleepPeriod(start, end, period.ongoing and end == period.end))
            start = end
    return dict(days)


@dataclass
class SleepDay:
    day: date
    periods: list[SleepPeriod]

    @property
    def total(self) -> float:
        return sum(p.seconds for p in self.periods)

    @property
    def longest(self) -> float:
        return max((p.seconds for p in self.periods), default=0.0)


def sleep_days(
    samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig, day_start: time
) -> list[SleepDay]:
    days = split_by_day(find_sleep(samples, cfg), day_start)
    return [SleepDay(day, periods) for day, periods in sorted(days.items())]


def period_lines(periods: list[SleepPeriod], now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    return [
        t(
            # A sleep cut off by the end of old data is not "now".
            "sleep.ongoing" if p.ongoing and now - p.end <= 2 * MINUTE else "sleep.period",
            start=f"{p.start:%H:%M}",
            end=f"{p.end:%H:%M}",
            duration=fmt_duration(p.seconds),
        )
        for p in periods
    ]


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
    out = [t("sleep.title", start=f"{day_start:%H:%M}")]
    for d in days:
        out.append(
            f"{d.day:%d.%m} ({weekdays[d.day.weekday()]})  "
            + t("sleep.day", total=fmt_duration(d.total), count=len(d.periods), longest=fmt_duration(d.longest))
        )
    for d in days[-detail_days:] if detail_days else []:
        out += ["", t("sleep.day_title", date=f"{d.day:%d.%m.%Y}")]
        out += [f"  {line}" for line in period_lines(d.periods)]
    return "\n".join(out)


def sleep_state(
    samples: list[tuple[datetime, Side, float | None]], cfg: SleepConfig, now: datetime
) -> tuple[bool | None, SleepPeriod | None]:
    """(asleep, current sleep) at `now`; (None, None) when there is no recent movement data."""
    recent = now - MAX_GAP
    if not any(m is not None for ts, _, m in samples if ts >= recent):
        return None, None
    periods = find_sleep(samples, cfg)
    if periods and periods[-1].ongoing and now - periods[-1].end <= 2 * MINUTE:
        return True, periods[-1]
    return False, None
