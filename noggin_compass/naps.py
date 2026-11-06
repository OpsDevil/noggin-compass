"""Sleep outside the crib (in arms, in a pram), entered by the parents in Telegram. The camera
cannot see it, so it is stored on its own and added to the sleep the camera found.

There are three ways to enter it:
* a button with its length: the nap is taken to have ended when the button was pressed;
* /nap 14:10-15:30, or /nap 14:10 for a nap from 14:10 till now;
* "Fell asleep" when the baby falls asleep on a walk and "Woke up" when it wakes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta

from .config import SleepConfig
from .i18n import t
from .report import fmt_duration
from .sleep import SLEEP_LOOKBACK, SleepPeriod, find_sleep, overlaps, spans_text
from .storage import Storage
from .telegram import Reply

DURATIONS = (15, 30, 45, 60, 90, 120, 150, 180)  # minutes, one button each
ROW = 4
SHOWN = 10  # entries in the list for corrections
_TIME = r"(\d{1,2})[:.](\d{2})"
# 14:10-15:30 or 14:10; a phone may turn the dash into a long one.
_SPAN = re.compile(rf"^{_TIME}(?:\s*[-\u2013\u2014]\s*{_TIME})?$")


def periods(
    storage: Storage, since: datetime | None = None, until: datetime | None = None, now: datetime | None = None
) -> list[SleepPeriod]:
    """The naps in the period, in time order. One still going on lasts till `now`."""
    now = now or datetime.now()
    return [
        SleepPeriod.nap(start, end) if end is not None else SleepPeriod.nap(start, max(now, start), ongoing=True)
        for _, start, end in reversed(storage.naps(since, until))
    ]


def parse_span(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """14:10-15:30 or 14:10 (till now) as the latest such span that has ended by `now`: a time
    later than now is yesterday's, and a start later than the end is the day before the end.
    None if it is not a time, or if the sleep would last less than a minute."""
    match = _SPAN.match(text.strip())
    if match is None:
        return None
    numbers = [int(n) for n in match.groups() if n is not None]
    if any(h > 23 or m > 59 for h, m in zip(numbers[::2], numbers[1::2])):
        return None
    now = now.replace(second=0, microsecond=0)

    def latest(hour: int, minute: int, before: datetime) -> datetime:
        moment = before.replace(hour=hour, minute=minute)
        return moment - timedelta(days=1) if moment > before else moment

    if len(numbers) == 2:
        start, end = latest(*numbers, now), now
    else:
        if numbers[:2] == numbers[2:]:
            return None
        end = latest(numbers[2], numbers[3], now)
        start = latest(numbers[0], numbers[1], end)
    return (start, end) if end > start else None


def _label(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return t("dur.minutes", m=rest)
    return t("nap.hours", h=hours) if not rest else t("dur.hours", h=hours, m=rest)


def _when(start: datetime, end: datetime | None, today: datetime) -> str:
    span = f"{start:%H:%M}-{t('nap.now') if end is None else f'{end:%H:%M}'}"
    last = end or today
    return span if last.date() == today.date() else f"{last:%d.%m} {span}"


class NapBook:
    def __init__(self, db_path: str, sleep: SleepConfig | None = None, clock: Callable[[], datetime] = datetime.now):
        self.db_path = db_path
        self.sleep = sleep or SleepConfig()
        self.clock = clock

    def _storage(self) -> Storage:
        return Storage(self.db_path)

    def _now(self) -> datetime:
        return self.clock().replace(second=0, microsecond=0)

    def _durations(self, action: str) -> list[list[tuple[str, str]]]:
        buttons = [(_label(m), f"{action}:{m}") for m in DURATIONS]
        return [buttons[i : i + ROW] for i in range(0, len(buttons), ROW)]

    def _describe(self, start: datetime, end: datetime | None) -> str:
        now = self.clock()
        return t("nap.entry", when=_when(start, end, now), duration=fmt_duration(((end or now) - start).total_seconds()))

    def _actions(self, nap_id: int, end: datetime | None) -> list[list[tuple[str, str]]]:
        if end is None:
            return [[(t("nap.btn.wake"), f"nap:wake:{nap_id}"), (t("nap.btn.delete"), f"nap:del:{nap_id}")]]
        return [[(t("nap.btn.change"), f"nap:edit:{nap_id}"), (t("nap.btn.delete"), f"nap:del:{nap_id}")]]

    def _open_text(self, start: datetime) -> str:
        return t("nap.open", since=f"{start:%H:%M}", duration=fmt_duration((self.clock() - start).total_seconds()))

    def _warnings(self, storage: Storage, nap_id: int, start: datetime, end: datetime) -> list[str]:
        """What the nap overlaps: sleep the camera saw in the crib, other entered naps. Both are
        counted once in the totals; the parents are told, since one of the two may be wrong."""
        crib = find_sleep(storage.activity(start - SLEEP_LOOKBACK, end), self.sleep)
        others = [SleepPeriod(s, e or max(self.clock(), s)) for i, s, e in storage.naps(start, end) if i != nap_id]
        lines = []
        if spans := overlaps((start, end), crib):
            lines.append(t("nap.overlaps_crib", spans=spans_text(spans)))
        if spans := overlaps((start, end), others):
            lines.append(t("nap.overlaps_nap", spans=spans_text(spans)))
        return lines

    def _saved(self, storage: Storage, key: str, nap_id: int, start: datetime, end: datetime) -> Reply:
        lines = [t(key, entry=self._describe(start, end)), *self._warnings(storage, nap_id, start, end)]
        return Reply("\n".join(lines), self._actions(nap_id, end))

    def current_line(self) -> str | None:
        """"Asleep outside the crib for ..., since ..." while a nap is open, for /now."""
        storage = self._storage()
        try:
            nap = storage.current_nap()
        finally:
            storage.close()
        if nap is None:
            return None
        return t("bot.now_nap", duration=fmt_duration((self.clock() - nap[1]).total_seconds()), since=f"{nap[1]:%H:%M}")

    def menu(self) -> Reply:
        storage = self._storage()
        try:
            nap = storage.current_nap()
        finally:
            storage.close()
        listing = [[(t("nap.btn.list"), "nap:list")]]
        if nap is not None:
            return Reply(self._open_text(nap[1]), self._actions(nap[0], None) + listing)
        return Reply(t("nap.ask"), self._durations("nap:add") + [[(t("nap.btn.sleep"), "nap:sleep")]] + listing)

    def command(self, args: str) -> Reply:
        """/nap with nothing after it: the buttons; with a time: that nap."""
        if not args.strip():
            return self.menu()
        span = parse_span(args, self.clock())
        if span is None:
            return Reply(t("nap.bad_time"))
        storage = self._storage()
        try:
            nap_id = storage.add_nap(*span)
            return self._saved(storage, "nap.added", nap_id, *span)
        finally:
            storage.close()

    def list(self) -> Reply:
        storage = self._storage()
        try:
            naps = storage.naps(limit=SHOWN)
        finally:
            storage.close()
        if not naps:
            return Reply(t("nap.list_empty"))
        rows = [[(self._describe(start, end), f"nap:show:{nap_id}")] for nap_id, start, end in naps]
        return Reply(t("nap.list"), rows)

    def press(self, data: str) -> Reply | None:
        """What replaces the message whose button was pressed. None for buttons of other features."""
        parts = data.split(":")
        if parts[0] != "nap" or len(parts) < 2:
            return None
        action, args = parts[1], parts[2:]
        try:
            numbers = [int(a) for a in args]
        except ValueError:
            return None
        if action == "list":
            return self.list()
        if action == "sleep":
            return self._fall_asleep()
        if action == "add" and len(numbers) == 1:
            end = self._now()
            return self._add(end - timedelta(minutes=numbers[0]), end)
        if not numbers:
            return None
        storage = self._storage()
        try:
            nap = storage.nap(numbers[0])
            if nap is None:
                # The other parent deleted it, or the message is old.
                return Reply(t("nap.gone"), [[(t("nap.btn.list"), "nap:list")]])
            nap_id, start, end = nap
            if action == "show":
                text = self._describe(start, end) if end is not None else self._open_text(start)
                return Reply(text, self._actions(nap_id, end) + [[(t("nap.btn.back"), "nap:list")]])
            if action == "del":
                storage.delete_nap(nap_id)
                return Reply(t("nap.deleted", entry=self._describe(start, end)), [[(t("nap.btn.list"), "nap:list")]])
            if action == "wake":
                if end is not None:  # the other parent pressed it first
                    return Reply(self._describe(start, end), self._actions(nap_id, end))
                end = self._now()
                if end <= start:
                    storage.delete_nap(nap_id)
                    return Reply(t("nap.too_short"))
                storage.close_nap(nap_id, end)
                return self._saved(storage, "nap.added", nap_id, start, end)
            if end is None:  # a nap still going on has no length to change yet
                return Reply(self._open_text(start), self._actions(nap_id, None))
            if action == "edit":
                return Reply(
                    t("nap.ask_again", entry=self._describe(start, end)),
                    self._durations(f"nap:set:{nap_id}") + [[(t("nap.btn.back"), f"nap:show:{nap_id}")]],
                )
            if action == "set" and len(numbers) == 2:
                # The end stays: it is when the parent said the baby woke up.
                start = end - timedelta(minutes=numbers[1])
                storage.update_nap(nap_id, start, end)
                return self._saved(storage, "nap.changed", nap_id, start, end)
            return None
        finally:
            storage.close()

    def _add(self, start: datetime, end: datetime) -> Reply:
        storage = self._storage()
        try:
            nap_id = storage.add_nap(start, end)
            return self._saved(storage, "nap.added", nap_id, start, end)
        finally:
            storage.close()

    def _fall_asleep(self) -> Reply:
        storage = self._storage()
        try:
            nap_id, new = storage.open_nap(self._now())
            start = storage.nap(nap_id)[1]
        finally:
            storage.close()
        text = t("nap.started", since=f"{start:%H:%M}") if new else self._open_text(start)
        return Reply(text, self._actions(nap_id, None))
