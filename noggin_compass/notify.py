"""What goes out to the parents: the "time to turn the baby" alert, the bot's answers, and the
one place every message, notification and sound leaves from."""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2

from .balance import Alert, BalanceMonitor
from .chart import DAYS_SHOWN, day_label, render_chart
from .config import Config
from .head import Side, side_label
from .i18n import hours_text, t
from .naps import NapBook
from .naps import periods as nap_periods
from .preview import Frame, fit, picture
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, segments_from, summarize
from .sleep import SLEEP_LOOKBACK, find_sleep, no_sleep_text, period_lines, with_naps, within
from .storage import Storage
from .telegram import Photo, Reply, TelegramBot, resolve_credentials
from .thermometer import climate_lines, tip_lines
from .tracker import Segment

log = logging.getLogger(__name__)

SYSTEM_SOUNDS = Path("/System/Library/Sounds")
# The bot answers about a rolling window: "the last 24 hours" needs no explaining, unlike a report
# day that starts at noon. Every text that names the window takes its hours from here.
RECENT_WINDOW = timedelta(hours=24)
SOUND_REPEATS = 3
# /now says the camera is silent when its last frame is older than this.
CAMERA_SILENT = timedelta(minutes=2)
# How often to look whether the camera has gone silent for camera.silent_minutes.
CAMERA_CHECK_SECONDS = 15
# The bot's commands, in the order of its menu and of its help.
COMMANDS = ("now", "stats", "sleep", "nap", "naps", "chart")
PHOTO_COMMANDS = ("photo", "marked")


def window_text() -> str:
    return hours_text(RECENT_WINDOW.total_seconds() / 3600)


def sound_path(sound: str) -> str:
    """Resolve a file path or a macOS system sound name (Funk, Glass, Sosumi, etc.) to a file."""
    if Path(sound).expanduser().is_file():
        return str(Path(sound).expanduser())
    return str(SYSTEM_SOUNDS / f"{sound}.aiff")


def check_sounds(cfg: Config) -> None:
    """Every sound the run may need exists: a typo in a name would otherwise mean an alarm that
    rings with no sound, found out on the night it is needed."""
    if sys.platform != "darwin":
        return
    wanted = [("alerts.sound_file", cfg.alerts.sound_file)] if cfg.alerts.sound else []
    if cfg.thermometers.devices:
        cold = cfg.thermometers.cold
        wanted += [("thermometers.cold.quiet_sound", cold.quiet_sound), ("thermometers.cold.alarm_sound", cold.alarm_sound)]
    for key, sound in wanted:
        if not Path(sound_path(sound)).is_file():
            raise SystemExit(t("err.sound", key=key, sound=sound))


_failed_sounds: set[str] = set()


def play(sound: str, volume: float = 1.0, times: int = 1) -> None:
    """Play a sound on this computer and wait until it ends. One that does not play is logged
    once, not failed silently every time."""
    path = sound_path(sound)
    for _ in range(times):
        try:
            done = subprocess.run(["afplay", "-v", f"{volume:g}", path], check=False, capture_output=True)
        except FileNotFoundError:  # afplay exists only on macOS
            return
        if done.returncode != 0:
            if path not in _failed_sounds:
                _failed_sounds.add(path)
                log.warning(t("notify.sound_failed", sound=sound))
            return


def mac_notification(text: str) -> None:
    text = text.replace("\\", "\\\\").replace('"', '\\"')  # escape for AppleScript
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{text}" with title "noggin-compass"'],
            check=False,
        )
    except FileNotFoundError:  # osascript exists only on macOS
        pass


class Outbox:
    """Everything that goes out to people, Telegram messages and notifications on the Mac, leaves
    from one thread of its own, in order. Telegram can take half a minute to answer without a
    network, and neither the camera nor the alarm's sound may wait for it."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)

    def put(self, how: Callable, *args) -> None:
        """Deliver from the thread once started; before that (the command line, tests) at once."""
        if self._thread is None:
            self._deliver(how, args)
        else:
            self._queue.put((how, args))

    def _run(self) -> None:
        while (letter := self._queue.get()) is not None:
            self._deliver(*letter)

    @staticmethod
    def _deliver(how: Callable, args: tuple) -> None:
        try:
            how(*args)
        except Exception:  # one lost message is better than all the ones after it
            log.exception(t("notify.failed"))


def format_alert(alert: Alert, window_hours: float) -> str:
    if alert.reason == "stint":
        return t(
            "alert.stint",
            side=side_label(alert.side),
            duration=fmt_duration(alert.stint_seconds),
            turn=side_label(alert.turn_to),
        )
    return t(
        "alert.imbalance",
        hours=f"{window_hours:g}",
        side=side_label(alert.side),
        more=fmt_duration(alert.totals[alert.side]),
        turn=side_label(alert.turn_to),
        less=fmt_duration(alert.totals[alert.turn_to]),
    )


@dataclass
class Status:
    """Latest state from the main loop; the bot reads it from its own thread."""

    time: datetime | None = None
    current: Segment | None = None
    stint: float = 0.0
    totals: dict[Side, float] = field(default_factory=dict)
    due: Alert | None = None
    # "Asleep for ..." or "Not asleep", worked out in the main loop for the picture too, so /now
    # and the picture say the same; None while the baby is out of the crib or nothing is known.
    asleep: str | None = None


def segments(samples: list, cfg: Config) -> list[Segment]:
    tracking = cfg.tracking
    return segments_from(samples, cfg.head.center_threshold, tracking.confirm_seconds, tracking.gap_seconds)


class Notifier:
    """`live` False is for a recorded video: alerts go to the log only, nothing to Telegram, and
    there is no camera to watch."""

    def __init__(self, cfg: Config, live: bool = True):
        self.cfg = cfg
        self.live = live
        self.status = Status()
        self.frame: Frame | None = None
        self.naps = NapBook(cfg.storage.db_path)
        self.climate = None  # a ClimateWatch, set by run() when there are thermometers
        self.outbox = Outbox()
        # The wall clock, not the last frame's time: with the camera silent, "the last 24 hours"
        # still end now, and /now can tell that its news is old.
        self.clock = datetime.now
        self.commands = COMMANDS + (PHOTO_COMMANDS if cfg.telegram.photos else ())
        self._camera_silent = False
        self._camera_lock = threading.Lock()
        self._stopped = threading.Event()
        self._started = self.clock()
        token, chat_ids = resolve_credentials(cfg.telegram.token, cfg.telegram.chat_ids) if live else (None, [])
        menu = [(name, self._describe(name)) for name in self.commands]
        self.bot = (
            TelegramBot(token, chat_ids, self.handle_command, commands=menu, on_button=self.press)
            if token
            else None
        )

    @staticmethod
    def _describe(command: str) -> str:
        return t(f"bot.cmd.{command}", window=window_text())

    def help_text(self) -> str:
        """Made from the same list as the bot's menu, so it never offers a command that is off."""
        lines = [f"/{name} - {self._describe(name)}" for name in self.commands]
        return "\n".join([t("bot.help_title"), *lines, t("bot.help_end")])

    def start(self) -> None:
        self.outbox.start()
        self._started = self.clock()
        if self.bot is not None:
            self.bot.start()
            log.info(t("tg.started", count=len(self.bot.chat_ids)))
        if self.live and self.cfg.camera.silent_minutes:
            threading.Thread(target=self._watch_camera, name="camera-watch", daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()
        if self.bot is not None:
            self.bot.stop()
        self.outbox.stop()

    def _watch_camera(self) -> None:
        while not self._stopped.wait(CAMERA_CHECK_SECONDS):
            self.check_camera()

    def check_camera(self) -> None:
        """Say once when the camera has gone silent: the run loop waits for frames then and could
        not tell anyone, so the parents would think the program is watching."""
        last = self.status.time or self._started
        if self.clock() - last < timedelta(minutes=self.cfg.camera.silent_minutes):
            return
        with self._camera_lock:
            if self._camera_silent:
                return
            self._camera_silent = True
        text = t("camera.silent", time=f"{last:%H:%M}")
        log.warning(text)
        self.notify(text)
        self.send(text)

    def send(self, text: str | Reply, silent: bool = False) -> None:
        if self.bot is not None:
            self.outbox.put(self.bot.send, text, None, silent)

    def notify(self, text: str) -> None:
        self.outbox.put(mac_notification, text)

    def press(self, data: str) -> str | Reply | Photo | None:
        if data.startswith("climate:"):
            return None if self.climate is None else self.climate.press(data)
        if data.startswith("chart:"):
            value = data.removeprefix("chart:")
            try:
                return self.chart(None if value == "recent" else date.fromisoformat(value))
            except ValueError:  # a button from an older version of the program
                return None
        return self.naps.press(data)

    def update(self, now: datetime, current: Segment | None, monitor: BalanceMonitor, asleep: str | None = None) -> None:
        self.status = Status(
            now, current, monitor.stint(now, current), monitor.totals(now, current), monitor.due(now, current), asleep
        )
        with self._camera_lock:
            back, self._camera_silent = self._camera_silent, False
        if back:
            log.info(t("camera.back"))
            self.send(t("camera.back"), silent=True)

    def set_frame(self, frame: Frame) -> None:
        self.frame = frame

    def alert(self, alert: Alert) -> None:
        text = format_alert(alert, self.cfg.alerts.window_hours)
        log.warning(text)
        if not self.live:  # a recorded video: what would have been said, in the log only
            return
        self.notify(text)
        if self.cfg.alerts.sound:
            threading.Thread(target=play, args=(self.cfg.alerts.sound_file, 1.0, SOUND_REPEATS), daemon=True).start()
        self.send(text)

    def handle_command(self, text: str) -> str | Photo | Reply:
        command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        if command == "/now":
            return self.now_text()
        if command == "/stats":
            return self.stats_text()
        if command == "/sleep":
            return self.sleep_text()
        if command == "/nap":
            return self.naps.menu()
        if command == "/naps":
            return self.naps.list()
        if command == "/chart":
            return self.chart()
        if command in ("/photo", "/marked"):
            return self.photo(marked=command == "/marked")
        return self.help_text()

    def photo(self, marked: bool) -> str | Photo:
        if not self.cfg.telegram.photos:
            return t("bot.photos_off")
        frame = self.frame
        if frame is None:
            return t("bot.no_data")
        image = picture(frame, self.cfg) if marked else frame.image
        ok, jpeg = cv2.imencode(".jpg", fit(image, 1600), [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return t("bot.no_data")
        score = f"{frame.estimate.score:+.2f}" if frame.estimate.score is not None else "-"
        caption = t("bot.photo_caption", time=f"{frame.time:%H:%M:%S}", side=side_label(frame.estimate.side), score=score)
        return Photo(jpeg.tobytes(), caption)

    def _window(self, since: datetime, until: datetime):
        """Head summary (None without data), the sleeps that overlap [since, until) (whole;
        `within` cuts them to it for the numbers), and the raw activity. A sleep that began earlier
        is found too."""
        storage = Storage(self.cfg.storage.db_path)
        try:
            samples = storage.samples(since, until)
            activity = storage.activity(since - SLEEP_LOOKBACK, until)
            naps = nap_periods(storage, since, until)
        finally:
            storage.close()
        found = segments(samples, self.cfg)
        summary = summarize(since.date(), found) if found else None
        sleeps = with_naps(find_sleep(activity, self.cfg.sleep), naps)
        periods = [p for p in sleeps if p.end > since and p.start < until]
        return summary, periods, activity

    def _recent(self, now: datetime):
        since = now - RECENT_WINDOW
        return (*self._window(since, now), since)

    def chart(self, day: date | None = None) -> str | Photo:
        """The last RECENT_WINDOW, or one calendar day, with buttons under it to see another day."""
        now = self.clock()
        day_start = parse_day_start(self.cfg.tracking.day_starts_at)
        if day is None:
            summary, periods, _, since = self._recent(now)
            until, title = now, t("chart.title_recent", window=window_text())
        else:
            since, end = day_bounds(day, day_start)
            until = min(end, now)
            summary, periods, _ = self._window(since, until)
            title = t("chart.title_day", date=day_label(day, day_start))
        # The per-day panel: the days up to the one shown.
        first = day_bounds(day_of(until, day_start) - timedelta(days=DAYS_SHOWN - 1), day_start)[0]
        storage = Storage(self.cfg.storage.db_path)
        try:
            samples = storage.samples(first, until)
        finally:
            storage.close()
        days = build_report(
            samples,
            center_threshold=self.cfg.head.center_threshold,
            confirm_seconds=self.cfg.tracking.confirm_seconds,
            gap_seconds=self.cfg.tracking.gap_seconds,
            day_start=day_start,
        )
        buttons = self._chart_buttons(day_of(now, day_start))
        clipped = [p for p in within(periods, since) if p.start < until]
        png = render_chart(summary, clipped, days, title, day_start)
        if png is None:
            empty = t("report.no_data") if day is None else t("chart.no_data_day", date=day_label(day, day_start))
            return Reply(empty, buttons)
        return Photo(png, t("bot.chart_caption", time=f"{now:%H:%M}"), filename="chart.png", buttons=buttons)

    @staticmethod
    def _chart_buttons(today: date) -> list[list[tuple[str, str]]]:
        days = [today - timedelta(days=n) for n in range(DAYS_SHOWN)]
        label = {0: t("chart.btn.today"), 1: t("chart.btn.yesterday")}
        named = [(label.get(n, f"{d:%d.%m}"), f"chart:{d.isoformat()}") for n, d in enumerate(days)]
        return [[(t("chart.btn.recent", window=window_text()), "chart:recent"), *named[:2]], named[2:]]

    def now_text(self) -> str:
        status = self.status
        if status.current is None:
            return t("bot.no_data")
        lines = []
        if self.clock() - status.time > CAMERA_SILENT:
            lines.append(t("bot.camera_silent", time=f"{status.time:%H:%M}"))
        side = status.current.side
        if side in (Side.LEFT, Side.RIGHT):
            lines.append(t("bot.now_side", side=side_label(side), duration=fmt_duration(status.stint)))
        else:
            lines.append(t(f"bot.now_{side.value}"))
        if status.totals:
            lines.append(
                t(
                    "bot.now_balance",
                    hours=f"{self.cfg.alerts.window_hours:g}",
                    left=fmt_duration(status.totals.get(Side.LEFT, 0)),
                    right=fmt_duration(status.totals.get(Side.RIGHT, 0)),
                )
            )
        if status.due is not None:
            lines.append(t("bot.turn", turn=side_label(status.due.turn_to)))
        elif side in (Side.LEFT, Side.RIGHT):
            lines.append(t("bot.ok"))
        if status.asleep:
            lines.append(status.asleep)
        return "\n".join(lines + self.climate_lines())

    def climate_lines(self) -> list[str]:
        """Readings with the battery, the alarm if there is one, and what to do about the heat or
        the humidity. The readings are the alarm's own, the ones the picture shows too."""
        thermometers = self.cfg.thermometers
        if not thermometers.devices:
            return []
        if self.climate is not None:
            readings = self.climate.readings()
        else:  # no run going on: what it saved
            storage = Storage(self.cfg.storage.db_path)
            try:
                readings = storage.latest_climate()
            finally:
                storage.close()
        now = self.clock()
        lines = climate_lines(thermometers.devices, readings, now, thermometers.every_minutes,
                              thermometers.low_battery_volts, battery=True)
        if self.climate is not None:
            lines += self.climate.status_lines()
        return lines + tip_lines(thermometers.devices, readings, thermometers.limits, now, thermometers.every_minutes)

    def sleep_text(self) -> str:
        now = self.clock()
        _, periods, activity, since = self._recent(now)
        if not periods:
            return no_sleep_text(activity)
        inside = within(periods, since)
        lines = [
            t(
                "bot.sleep",
                window=window_text(),
                total=fmt_duration(sum(p.seconds for p in inside)),
                count=len(periods),
                longest=fmt_duration(max(p.seconds for p in inside)),
            )
        ]
        return "\n".join(lines + period_lines(periods, now))

    def stats_text(self) -> str:
        now = self.clock()
        s, periods, _, since = self._recent(now)
        if s is None:
            return t("report.no_data")
        text = t(
            "bot.stats",
            window=window_text(),
            left=fmt_duration(s.totals[Side.LEFT]),
            left_pct=f"{s.share(Side.LEFT):.0%}",
            right=fmt_duration(s.totals[Side.RIGHT]),
            right_pct=f"{s.share(Side.RIGHT):.0%}",
            center=fmt_duration(s.totals[Side.CENTER]),
            center_pct=f"{s.share(Side.CENTER):.0%}",
            unknown=fmt_duration(s.totals[Side.UNKNOWN]),
            turns=s.turns,
        )
        if periods:
            total = sum(p.seconds for p in within(periods, since))
            text += "\n" + t("bot.stats_sleep", total=fmt_duration(total), count=len(periods))
        return text


def load_history(storage: Storage, cfg: Config, until: datetime) -> list[Segment]:
    """Segments from the last window_hours in the database, smoothed as in the report, so that
    after a restart the program remembers how long the head has already been on each side."""
    since = until - timedelta(hours=cfg.alerts.window_hours)
    return segments(storage.samples(since, until), cfg)


def monitor_from_config(cfg: Config) -> BalanceMonitor:
    a = cfg.alerts
    return BalanceMonitor(
        max_same_side=timedelta(minutes=a.max_same_side_minutes),
        max_imbalance=timedelta(minutes=a.max_imbalance_minutes),
        window=timedelta(hours=a.window_hours),
        min_turn=timedelta(minutes=a.min_turn_minutes),
        repeat=timedelta(minutes=a.repeat_minutes),
    )
