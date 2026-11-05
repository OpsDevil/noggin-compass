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
# Without movement data this recent, the bot does not say whether the baby is asleep.
RECENT = timedelta(minutes=5)


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
            days[day].append(SleepPeriod(start, end, period.ongoing and end == period.end, period.manual))
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
            "sleep.ongoing" if p.ongoing and now - p.end <= 2 * MINUTE else "sleep.period",
            start=f"{p.start:%H:%M}",
            end=f"{p.end:%H:%M}",
            duration=fmt_duration(p.seconds),
        )
        lines.append(t("sleep.manual", line=line) if p.manual else line)
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
    recent = now - RECENT
    if not any(m is not None for ts, _, m in samples if ts >= recent):
        return None, None
    periods = find_sleep(samples, cfg)
    if periods and periods[-1].ongoing and now - periods[-1].end <= 2 * MINUTE:
        return True, periods[-1]
    return False, None
