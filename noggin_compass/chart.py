"""A picture for Telegram and the report: time per side, a timeline of the head and sleep, and
left versus right over the last days."""

from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle

from .head import Side, side_label
from .i18n import t
from .report import DaySummary, fmt_duration
from .sleep import SleepPeriod

# Colours checked for colour-blind safety as a set (blue, orange, aqua).
COLORS = {Side.LEFT: "#2a78d6", Side.RIGHT: "#eb6834", Side.CENTER: "#1baf7a", Side.UNKNOWN: "#d6d5d0"}
SLEEP_COLOR = "#52514e"
# Sleep outside the crib, entered by the parents: apart from the sides by hue and from the crib
# sleep by lightness.
OUTSIDE_COLOR = "#a77fd8"
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e5e4df"
SHOWN = (Side.LEFT, Side.CENTER, Side.RIGHT)
DAYS_SHOWN = 7
WIDTH = 10  # inches; 1280 px at 128 dpi, the size Telegram keeps
LEFT, RIGHT = 0.09, 0.97


def _hour_floor(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=10, length=0)


def day_label(day: date, day_start: time) -> str:
    """A report day that starts at noon holds the night from one date to the next: 04-05.11."""
    if day_start == time(0, 0):
        return f"{day:%d.%m}"
    following = day + timedelta(days=1)
    return f"{day:%d}-{following:%d.%m}" if day.month == following.month else f"{day:%d.%m}-{following:%d.%m}"


def _header(fig: Figure, ax, day: DaySummary, title: str, top: float) -> None:
    """Title, a legend that carries the totals, and one bar split by side."""
    _style(ax)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.text(LEFT, top, title, color=TEXT, fontsize=14, fontweight="bold", va="top")
    renderer = fig.canvas.get_renderer()
    x = LEFT
    items = [(side, f"{side_label(side)} {fmt_duration(day.totals[side])} ({day.share(side):.0%})") for side in SHOWN]
    if day.totals[Side.UNKNOWN]:
        items.append((Side.UNKNOWN, f"{side_label(Side.UNKNOWN)} {fmt_duration(day.totals[Side.UNKNOWN])}"))
    legend_y = top - 0.38 / fig.get_figheight()
    for side, label in items:
        fig.patches.append(Rectangle(
            (x, legend_y - 0.07 / fig.get_figheight()), 0.012, 0.16 / fig.get_figheight(),
            color=COLORS[side], lw=0, transform=fig.transFigure,
        ))
        text = fig.text(x + 0.018, legend_y, label, color=TEXT, fontsize=11, va="center")
        width = text.get_window_extent(renderer).width / fig.bbox.width
        x += 0.018 + width + 0.03
    tracked = day.tracked_seconds
    left = 0.0
    for side in SHOWN:
        width = day.totals[side] / tracked if tracked else 0.0
        if width > 0:
            # A thin gap between segments keeps neighbouring colours apart.
            ax.add_patch(Rectangle((left, 0), max(width - 0.003, 0.0), 1, color=COLORS[side], lw=0))
        left += width
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)


def _timeline(ax, day: DaySummary, sleep: list[SleepPeriod]) -> None:
    _style(ax)
    start = _hour_floor(min(s.start for s in day.segments))
    # A nap the parents logged can end after the last frame from the camera.
    last = max([s.end for s in day.segments] + [p.end for p in sleep])
    end = _hour_floor(last) + timedelta(hours=1)

    def x(ts: datetime) -> float:
        return (ts - start).total_seconds() / 3600

    for seg in day.segments:
        if seg.side in COLORS:
            ax.broken_barh([(x(seg.start), seg.seconds / 3600)], (1.1, 0.8), color=COLORS[seg.side], lw=0)
    def bar(begin: datetime, finish: datetime, color: str) -> None:
        begin, finish = max(begin, start), min(finish, end)
        if finish > begin:
            ax.broken_barh([(x(begin), (finish - begin).total_seconds() / 3600)], (0.1, 0.8), color=color, lw=0)

    for period in sleep:
        bar(period.start, period.end, SLEEP_COLOR)
        for begin, finish in period.outside:
            bar(begin, finish, OUTSIDE_COLOR)
    if any(period.outside for period in sleep):
        ax.legend(
            handles=[Patch(color=SLEEP_COLOR, label=t("chart.in_crib")), Patch(color=OUTSIDE_COLOR, label=t("chart.outside"))],
            loc="lower right", bbox_to_anchor=(1, 1), ncol=2, frameon=False, fontsize=10,
            labelcolor=TEXT_SECONDARY, handlelength=1, borderaxespad=0.2,
        )
    hours = int(round(x(end)))
    step = 1 if hours <= 12 else 2
    ax.set_xticks(range(0, hours + 1, step))
    ax.set_xticklabels([f"{start + timedelta(hours=h):%H:%M}" for h in range(0, hours + 1, step)])
    ax.set_xlim(0, hours)
    ax.set_ylim(0, 2)
    ax.set_yticks([0.5, 1.5])
    ax.set_yticklabels([t("chart.sleep"), t("chart.head")], color=TEXT, fontsize=11)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title(t("chart.timeline"), loc="left", color=TEXT_SECONDARY, fontsize=11)


def _days(ax, summaries: list[DaySummary], day_start: time) -> None:
    """Left to the left, right to the right, one row per day, the latest on top."""
    _style(ax)
    days = summaries[-DAYS_SHOWN:][::-1]
    longest = max(max(d.totals[Side.LEFT], d.totals[Side.RIGHT]) for d in days) / 3600 or 1.0
    for row, day in enumerate(days):
        left, right = day.totals[Side.LEFT] / 3600, day.totals[Side.RIGHT] / 3600
        ax.barh(row, -left, height=0.6, color=COLORS[Side.LEFT], lw=0)
        ax.barh(row, right, height=0.6, color=COLORS[Side.RIGHT], lw=0)
        pad = longest * 0.02
        ax.text(-left - pad, row, fmt_duration(day.totals[Side.LEFT]), ha="right", va="center", color=TEXT, fontsize=10)
        ax.text(right + pad, row, fmt_duration(day.totals[Side.RIGHT]), ha="left", va="center", color=TEXT, fontsize=10)
    ax.axvline(0, color=TEXT_SECONDARY, lw=1)
    ax.set_yticks(range(len(days)))
    ax.set_yticklabels([day_label(d.day, day_start) for d in days], color=TEXT, fontsize=10)
    ax.set_xlim(-longest * 1.45, longest * 1.45)
    ax.set_ylim(len(days) - 0.4, -0.6)
    ax.set_xticks([])
    ax.set_title(
        t("chart.days", left=side_label(Side.LEFT), right=side_label(Side.RIGHT)),
        loc="left", color=TEXT_SECONDARY, fontsize=11,
    )


def render_chart(
    day: DaySummary | None,
    sleep: list[SleepPeriod],
    days: list[DaySummary],
    title: str,
    day_start: time,
) -> bytes | None:
    """PNG with `day` on top (the last 24 hours or one report day), its sleep, and left versus
    right for `days`. None if there is no data."""
    if day is None or not day.segments:
        return None
    shown_days = min(len(days), DAYS_SHOWN)
    # Heights in inches, top to bottom.
    header, bar, timeline = 0.75, 0.3, 1.9
    days_height = 0.36 * shown_days + 0.7 if shown_days > 1 else 0.0
    height = 0.25 + header + bar + 0.45 + timeline + days_height + 0.1
    fig = Figure(figsize=(WIDTH, height), dpi=128, facecolor=SURFACE)
    FigureCanvasAgg(fig)

    def axes(top: float, size: float):
        return fig.add_axes((LEFT, 1 - (top + size) / height, RIGHT - LEFT, size / height))

    _header(fig, axes(0.25 + header, bar), day, title, top=1 - 0.2 / height)
    y = 0.25 + header + bar + 0.45
    _timeline(axes(y + 0.3, timeline - 0.65), day, sleep)
    if days_height:
        _days(axes(y + timeline + 0.35, days_height - 0.45), days, day_start)
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=SURFACE)
    return out.getvalue()
