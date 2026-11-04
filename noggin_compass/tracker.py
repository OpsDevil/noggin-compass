"""Smooths raw estimates into continuous segments of "head turned to a given side"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .head import Side


@dataclass
class Segment:
    side: Side
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class SideTracker:
    """A side change counts only once the new position has held for confirm_seconds, so one or
    two bad frames from the detector do not break a segment. The change is dated to the first
    frame in the new position. If there are no frames for longer than gap_seconds, the segment
    ends at the last frame and the gap is not counted anywhere."""

    def __init__(self, confirm_seconds: float = 10.0, gap_seconds: float = 60.0):
        self.confirm_seconds = confirm_seconds
        self.gap_seconds = gap_seconds
        self._side: Side | None = None
        self._start: datetime | None = None
        self._last: datetime | None = None
        self._candidate: Side | None = None
        self._candidate_since: datetime | None = None

    @property
    def current(self) -> Segment | None:
        if self._side is None:
            return None
        return Segment(self._side, self._start, self._last)

    def update(self, ts: datetime, side: Side) -> list[Segment]:
        closed: list[Segment] = []
        if self._last is not None and (ts - self._last).total_seconds() > self.gap_seconds:
            closed += self.finish()

        if self._side is None:
            self._side, self._start = side, ts
        elif side == self._side:
            self._candidate = None
        else:
            if side != self._candidate:
                self._candidate, self._candidate_since = side, ts
            if (ts - self._candidate_since).total_seconds() >= self.confirm_seconds:
                closed.append(Segment(self._side, self._start, self._candidate_since))
                self._side, self._start = side, self._candidate_since
                self._candidate = None
        self._last = ts
        return closed

    def finish(self) -> list[Segment]:
        segment = self.current
        self._side = self._start = self._last = self._candidate = self._candidate_since = None
        return [segment] if segment is not None else []


def build_segments(
    samples: list[tuple[datetime, Side]], confirm_seconds: float, gap_seconds: float
) -> list[Segment]:
    tracker = SideTracker(confirm_seconds, gap_seconds)
    segments: list[Segment] = []
    for ts, side in samples:
        segments += tracker.update(ts, side)
    segments += tracker.finish()
    return [s for s in segments if s.seconds > 0]
