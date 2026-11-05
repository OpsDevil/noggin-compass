"""Sleep outside the crib (in arms, in a pram), entered by the parents with buttons in Telegram.
The camera cannot see it, so it is stored on its own and added to the sleep the camera found.

A nap is entered by its length and is taken to have ended when the button was pressed: a parent
does it right after the baby wakes up, and a few minutes either way do not change the day's total.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from .i18n import t
from .report import fmt_duration
from .sleep import SleepPeriod
from .storage import Storage
from .telegram import Reply

DURATIONS = (15, 30, 45, 60, 90, 120, 150, 180)  # minutes, one button each
ROW = 4
SHOWN = 10  # entries in the list for corrections


def periods(storage: Storage, since: datetime | None = None, until: datetime | None = None) -> list[SleepPeriod]:
    return [SleepPeriod(start, end, manual=True) for _, start, end in reversed(storage.naps(since, until))]


def _label(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return t("dur.minutes", m=rest)
    return t("nap.hours", h=hours) if not rest else t("dur.hours", h=hours, m=rest)


def _when(start: datetime, end: datetime, today: datetime) -> str:
    span = f"{start:%H:%M}-{end:%H:%M}"
    return span if end.date() == today.date() else f"{end:%d.%m} {span}"


class NapBook:
    def __init__(self, db_path: str, clock: Callable[[], datetime] = datetime.now):
        self.db_path = db_path
        self.clock = clock

    def _storage(self) -> Storage:
        return Storage(self.db_path)

    def _durations(self, action: str) -> list[list[tuple[str, str]]]:
        buttons = [(_label(m), f"{action}:{m}") for m in DURATIONS]
        return [buttons[i : i + ROW] for i in range(0, len(buttons), ROW)]

    def _describe(self, start: datetime, end: datetime) -> str:
        return t("nap.entry", when=_when(start, end, self.clock()), duration=fmt_duration((end - start).total_seconds()))

    def _actions(self, nap_id: int) -> list[list[tuple[str, str]]]:
        return [[(t("nap.btn.change"), f"nap:edit:{nap_id}"), (t("nap.btn.delete"), f"nap:del:{nap_id}")]]

    def menu(self) -> Reply:
        return Reply(t("nap.ask"), self._durations("nap:add") + [[(t("nap.btn.list"), "nap:list")]])

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
        if action == "add" and len(numbers) == 1:
            return self._add(numbers[0])
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
                return Reply(self._describe(start, end), self._actions(nap_id) + [[(t("nap.btn.back"), "nap:list")]])
            if action == "edit":
                return Reply(
                    t("nap.ask_again", entry=self._describe(start, end)),
                    self._durations(f"nap:set:{nap_id}") + [[(t("nap.btn.back"), f"nap:show:{nap_id}")]],
                )
            if action == "set" and len(numbers) == 2:
                # The end stays: it is when the parent said the baby woke up.
                start = end - timedelta(minutes=numbers[1])
                storage.update_nap(nap_id, start, end)
                return Reply(t("nap.changed", entry=self._describe(start, end)), self._actions(nap_id))
            if action == "del":
                storage.delete_nap(nap_id)
                return Reply(t("nap.deleted", entry=self._describe(start, end)), [[(t("nap.btn.list"), "nap:list")]])
            return None
        finally:
            storage.close()

    def _add(self, minutes: int) -> Reply:
        end = self.clock().replace(second=0, microsecond=0)
        start = end - timedelta(minutes=minutes)
        storage = self._storage()
        try:
            nap_id = storage.add_nap(start, end)
        finally:
            storage.close()
        return Reply(t("nap.added", entry=self._describe(start, end)), self._actions(nap_id))
