"""When to turn the baby: the head should not lie on one side for too long, and the two sides
should stay roughly balanced over the last few hours."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .head import Side
from .tracker import Segment

TURN_SIDES = (Side.LEFT, Side.RIGHT)
# Out of the crib (or no data) for this long means a new sleep, and the stint starts over.
NEW_SLEEP_AFTER = timedelta(minutes=30)


def other(side: Side) -> Side:
    return Side.RIGHT if side == Side.LEFT else Side.LEFT


@dataclass
class Alert:
    reason: str  # "stint": too long on one side in a row; "imbalance": one-sided over the window
    side: Side  # where the head is now
    turn_to: Side
    stint_seconds: float
    totals: dict[Side, float]  # seconds on each side within the window


class BalanceMonitor:
    """Two rules, each disabled by a zero value:

    * a stint on one side lasts longer than max_same_side. Center and unknown do not break the
      stint, and neither does a turn to the other side shorter than min_turn: that is not long
      enough to take the pressure off the back of the head;
    * within the last `window`, one side has collected max_imbalance more time than the other,
      and the head is on that side again now.

    While the condition holds, the alert repeats every `repeat`. As soon as the head moves to
    the other side, the repeat timer resets."""

    def __init__(
        self,
        max_same_side: timedelta,
        max_imbalance: timedelta,
        window: timedelta,
        min_turn: timedelta,
        repeat: timedelta,
    ):
        self.max_same_side = max_same_side
        self.max_imbalance = max_imbalance
        self.window = window
        self.min_turn = min_turn
        self.repeat = repeat
        self._segments: list[Segment] = []
        self._last_alert: datetime | None = None
        self._last_alert_side: Side | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.max_same_side or self.max_imbalance)

    def add(self, closed: list[Segment]) -> None:
        self._segments += closed

    def _history(self, now: datetime, current: Segment | None) -> list[Segment]:
        segments = self._segments + ([current] if current is not None else [])
        start = now - self.window
        self._segments = [s for s in self._segments if s.end > start]
        return [s for s in segments if s.end > start]

    def totals(self, now: datetime, current: Segment | None) -> dict[Side, float]:
        start = now - self.window
        totals = {side: 0.0 for side in TURN_SIDES}
        for seg in self._history(now, current):
            if seg.side in totals:
                totals[seg.side] += (seg.end - max(seg.start, start)).total_seconds()
        return totals

    def stint(self, now: datetime, current: Segment | None) -> float:
        """Seconds the head has spent on current.side in the ongoing stint."""
        if current is None or current.side not in TURN_SIDES:
            return 0.0
        side, total, later_start = current.side, 0.0, now
        for seg in reversed(self._history(now, current)):
            # A long gap in the data (the program was not running): what happened then is unknown.
            if later_start - seg.end >= NEW_SLEEP_AFTER:
                break
            length = seg.end - seg.start
            if seg.side == other(side) and length >= self.min_turn:
                break
            if seg.side == Side.ABSENT and length >= NEW_SLEEP_AFTER:
                break
            if seg.side == side:
                total += length.total_seconds()
            later_start = seg.start
        return total

    def due(self, now: datetime, current: Segment | None) -> Alert | None:
        """Whether the baby should be turned now, ignoring the alert repeat interval."""
        if not self.enabled or current is None or current.side not in TURN_SIDES:
            return None
        side = current.side
        totals = self.totals(now, current)
        stint = self.stint(now, current)
        if self.max_same_side and stint >= self.max_same_side.total_seconds():
            reason = "stint"
        elif self.max_imbalance and (
            totals[side] - totals[other(side)] >= self.max_imbalance.total_seconds()
        ):
            reason = "imbalance"
        else:
            return None
        return Alert(reason, side, other(side), stint, totals)

    def check(self, now: datetime, current: Segment | None) -> Alert | None:
        """Like due, but at most once per `repeat` while the head stays on the same side."""
        alert = self.due(now, current)
        if alert is None:
            return None
        if alert.side != self._last_alert_side:
            self._last_alert = None
        if self._last_alert is not None and now - self._last_alert < self.repeat:
            return None
        self._last_alert, self._last_alert_side = now, alert.side
        return alert
