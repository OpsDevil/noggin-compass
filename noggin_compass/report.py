"""Report: how long the head was turned left, right or straight, per day."""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .head import Side, classify, side_label
from .i18n import t
from .tracker import Segment, build_segments

TRACKED = (Side.LEFT, Side.RIGHT, Side.CENTER)
_COLORS_ORDER = (Side.LEFT, Side.RIGHT, Side.CENTER, Side.UNKNOWN, Side.ABSENT)
_COLORS = {Side.LEFT: "34", Side.RIGHT: "33", Side.CENTER: "32", Side.UNKNOWN: "90", Side.ABSENT: "90"}


@dataclass
class DaySummary:
    day: date
    totals: dict[Side, float] = field(default_factory=lambda: defaultdict(float))
    longest: dict[Side, float] = field(default_factory=lambda: defaultdict(float))
    turns: int = 0
    segments: list[Segment] = field(default_factory=list)

    @property
    def tracked_seconds(self) -> float:
        return sum(self.totals[s] for s in TRACKED)

    def share(self, side: Side) -> float:
        total = self.tracked_seconds
        return self.totals[side] / total if total else 0.0


def parse_day_start(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def day_of(ts: datetime, day_start: time) -> date:
    return (ts - timedelta(hours=day_start.hour, minutes=day_start.minute)).date()


def day_bounds(day: date, day_start: time) -> tuple[datetime, datetime]:
    start = datetime.combine(day, day_start)
    return start, start + timedelta(days=1)


def reclassify(
    samples: list[tuple[datetime, Side, float | None]], center_threshold: float
) -> list[tuple[datetime, Side]]:
    """Recompute the side from score so that a new threshold also applies to old data."""
    return [
        (ts, classify(score, center_threshold) if score is not None else side)
        for ts, side, score in samples
    ]


def segments_from(
    samples: list[tuple[datetime, Side, float | None]],
    center_threshold: float,
    confirm_seconds: float,
    gap_seconds: float,
) -> list[Segment]:
    """Stored samples to confirmed segments, the scores classified with today's center_threshold.
    The report, the bot, the chart and the restart all count head time from this one place."""
    return build_segments(reclassify(samples, center_threshold), confirm_seconds, gap_seconds)


def split_by_day(segments: list[Segment], day_start: time) -> dict[date, list[Segment]]:
    days: dict[date, list[Segment]] = defaultdict(list)
    for seg in segments:
        start = seg.start
        while start < seg.end:
            day = day_of(start, day_start)
            end = min(seg.end, day_bounds(day, day_start)[1])
            days[day].append(Segment(seg.side, start, end))
            start = end
    return dict(days)


def summarize(day: date, segments: list[Segment]) -> DaySummary:
    summary = DaySummary(day, segments=segments)
    previous: Side | None = None
    for seg in segments:
        summary.totals[seg.side] += seg.seconds
        summary.longest[seg.side] = max(summary.longest[seg.side], seg.seconds)
        if seg.side in TRACKED:
            if previous is not None and seg.side != previous:
                summary.turns += 1
            previous = seg.side
    return summary


def build_report(
    samples: list[tuple[datetime, Side, float | None]],
    *,
    center_threshold: float,
    confirm_seconds: float,
    gap_seconds: float,
    day_start: time,
) -> list[DaySummary]:
    segments = segments_from(samples, center_threshold, confirm_seconds, gap_seconds)
    return [summarize(day, segs) for day, segs in sorted(split_by_day(segments, day_start).items())]


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return t("dur.seconds", s=f"{seconds:.0f}")
    minutes = round(seconds / 60)
    if minutes < 60:
        return t("dur.minutes", m=minutes)
    return t("dur.hours", h=minutes // 60, m=minutes % 60)


def _symbol(side: Side) -> str:
    if side in (Side.LEFT, Side.RIGHT):
        return t(f"sym.{side.value}")
    return {Side.CENTER: "·", Side.UNKNOWN: "?", Side.ABSENT: "_"}[side]


def _paint(text: str, side: Side, color: bool) -> str:
    return f"\033[{_COLORS[side]}m{text}\033[0m" if color else text


def timeline(summary: DaySummary, day_start: time, color: bool) -> list[str]:
    """One line per hour, one character per minute: the side that dominated that minute."""
    start, _ = day_bounds(summary.day, day_start)
    minutes: dict[int, dict[Side, float]] = defaultdict(lambda: defaultdict(float))
    for seg in summary.segments:
        moment = seg.start
        while moment < seg.end:
            index = int((moment - start).total_seconds() // 60)
            bucket_end = min(seg.end, start + timedelta(minutes=index + 1))
            minutes[index][seg.side] += (bucket_end - moment).total_seconds()
            moment = bucket_end

    lines = []
    for hour in range(24):
        if not any(hour * 60 + m in minutes for m in range(60)):
            continue
        cells = []
        for m in range(60):
            bucket = minutes.get(hour * 60 + m)
            if not bucket:
                cells.append(" ")
                continue
            side = max(bucket, key=bucket.get)
            cells.append(_paint(_symbol(side), side, color))
        label = (start + timedelta(hours=hour)).strftime("%H:%M")
        lines.append(f"  {label} │{''.join(cells)}│")
    return lines


def render(summaries: list[DaySummary], day_start: time, timeline_days: int = 1) -> str:
    color = sys.stdout.isatty()
    if not summaries:
        return t("report.no_data")

    weekdays = t("weekdays").split(",")
    columns = [side_label(side) for side in TRACKED]
    out = [
        t("report.day_note_midnight") if day_start == time(0, 0) else t("report.day_note", start=f"{day_start:%H:%M}"),
        "",
        f"{t('report.col.day'):<11}"
        + "".join(f"{c:>16}" for c in columns)
        + f"{side_label(Side.UNKNOWN):>14}{t('report.col.turns'):>11}",
    ]
    for s in summaries:
        cols = [f"{fmt_duration(s.totals[side])} {s.share(side):4.0%}" for side in TRACKED]
        out.append(
            f"{s.day:%d.%m} ({weekdays[s.day.weekday()]})"
            + "".join(f"{c:>16}" for c in cols)
            + f"{fmt_duration(s.totals[Side.UNKNOWN]):>14}{s.turns:>11}"
        )

    for s in summaries[-timeline_days:] if timeline_days else []:
        out += ["", t("report.day_title", date=f"{s.day:%d.%m.%Y}")]
        for side in (*TRACKED, Side.UNKNOWN, Side.ABSENT):
            if s.totals[side]:
                out.append(
                    f"  {side_label(side):<14}{fmt_duration(s.totals[side]):>12}"
                    f"   {t('report.longest', duration=fmt_duration(s.longest[side]))}"
                )
        left, right = s.share(Side.LEFT), s.share(Side.RIGHT)
        if left + right:
            more = side_label(Side.LEFT if left > right else Side.RIGHT)
            out.append("  " + t("report.difference", pct=f"{abs(left - right):.0%}", side=more))
        out += ["", "  " + t("report.timeline")]
        out += timeline(s, day_start, color)
        legend = "  ".join(
            f"{_paint(_symbol(side), side, color)} {side_label(side)}" for side in _COLORS_ORDER
        )
        out.append(f"  {legend}")
    return "\n".join(out)
