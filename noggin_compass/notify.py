"""The "time to turn the baby" alert: a sound and a notification on the Mac, a Telegram message.
Also the bot's replies to /now and /stats."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import cv2
import numpy as np

from .balance import Alert, BalanceMonitor
from .chart import render_chart
from .config import Config
from .head import HeadEstimate, Side, side_label
from .i18n import t
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, reclassify, summarize
from .naps import NapBook
from .naps import periods as nap_periods
from .sleep import find_sleep, no_sleep_text, period_lines, sleep_state, with_naps
from .pose import Person
from .preview import draw, fit
from .storage import Storage
from .telegram import Photo, Reply, TelegramBot, resolve_credentials
from .thermometer import climate_lines
from .tracker import Segment, build_segments

log = logging.getLogger(__name__)

SYSTEM_SOUNDS = Path("/System/Library/Sounds")
# The bot answers about a rolling window: "the last 24 hours" needs no explaining, unlike a report
# day that starts at noon.
RECENT_WINDOW = timedelta(hours=24)
# Days on the chart's "left and right per day" panel.
CHART_DAYS = 7
# How far back sleep is looked for, to see a sleep that began before the period asked about.
SLEEP_LOOKBACK = timedelta(hours=12)
SOUND_REPEATS = 3


def sound_path(sound: str) -> str:
    """Resolve a file path or a macOS system sound name (Funk, Glass, Sosumi, etc.) to a file."""
    if Path(sound).expanduser().is_file():
        return str(Path(sound).expanduser())
    return str(SYSTEM_SOUNDS / f"{sound}.aiff")


def play_sound(sound: str) -> None:
    path = sound_path(sound)

    def play():
        for _ in range(SOUND_REPEATS):
            try:
                subprocess.run(["afplay", path], check=False)
            except FileNotFoundError:  # afplay exists only on macOS
                return

    threading.Thread(target=play, daemon=True).start()


def mac_notification(text: str) -> None:
    text = text.replace("\\", "\\\\").replace('"', '\\"')  # escape for AppleScript
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{text}" with title "noggin-compass"'],
            check=False,
        )
    except FileNotFoundError:  # osascript exists only on macOS
        pass


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


@dataclass
class Frame:
    """The latest analysed frame, for /photo and /marked."""

    image: np.ndarray
    person: Person | None
    estimate: HeadEstimate
    time: datetime


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.status = Status()
        self.frame: Frame | None = None
        self.naps = NapBook(cfg.storage.db_path)
        token, chat_ids = resolve_credentials(cfg.telegram.token, cfg.telegram.chat_ids)
        commands = [
            ("now", t("bot.cmd.now")),
            ("stats", t("bot.cmd.stats")),
            ("sleep", t("bot.cmd.sleep")),
            ("nap", t("bot.cmd.nap")),
            ("naps", t("bot.cmd.naps")),
            ("chart", t("bot.cmd.chart")),
        ]
        if cfg.telegram.photos:
            commands += [("photo", t("bot.cmd.photo")), ("marked", t("bot.cmd.marked"))]
        self.bot = (
            TelegramBot(token, chat_ids, self.handle_command, commands=commands, on_button=self.naps.press)
            if token
            else None
        )

    def start(self) -> None:
        if self.bot is not None:
            self.bot.start()
            log.info(t("tg.started", count=len(self.bot.chat_ids)))

    def stop(self) -> None:
        if self.bot is not None:
            self.bot.stop()

    def update(self, now: datetime, current: Segment | None, monitor: BalanceMonitor) -> None:
        self.status = Status(
            now, current, monitor.stint(now, current), monitor.totals(now, current), monitor.due(now, current)
        )

    def set_frame(self, image: np.ndarray, person: Person | None, estimate: HeadEstimate, time: datetime) -> None:
        self.frame = Frame(image, person, estimate, time)

    def alert(self, alert: Alert) -> None:
        text = format_alert(alert, self.cfg.alerts.window_hours)
        log.warning(text)
        mac_notification(text)
        if self.cfg.alerts.sound:
            play_sound(self.cfg.alerts.sound_file)
        if self.bot is not None:
            self.bot.send(text)

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
        return t("bot.help")

    def photo(self, marked: bool) -> str | Photo:
        if not self.cfg.telegram.photos:
            return t("bot.photos_off")
        frame = self.frame
        if frame is None:
            return t("bot.no_data")
        image = frame.image
        if marked:
            image = draw(image, frame.person, frame.estimate, self.cfg.detector.min_keypoint_conf, self.cfg.head.mirrored)
        ok, jpeg = cv2.imencode(".jpg", fit(image, 1600), [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return t("bot.no_data")
        score = f"{frame.estimate.score:+.2f}" if frame.estimate.score is not None else "-"
        caption = t("bot.photo_caption", time=f"{frame.time:%H:%M:%S}", side=side_label(frame.estimate.side), score=score)
        return Photo(jpeg.tobytes(), caption)

    def _recent(self, now: datetime):
        """The last 24 hours: head summary (None without data), sleeps that overlap the window,
        and the raw activity. A sleep that began earlier is found too."""
        since = now - RECENT_WINDOW
        storage = Storage(self.cfg.storage.db_path)
        try:
            samples = storage.samples(since, now)
            activity = storage.activity(since - SLEEP_LOOKBACK, now)
            naps = nap_periods(storage, since, now)
        finally:
            storage.close()
        segments = build_segments(
            reclassify(samples, self.cfg.head.center_threshold),
            self.cfg.tracking.confirm_seconds,
            self.cfg.tracking.gap_seconds,
        )
        summary = summarize(since.date(), segments) if segments else None
        periods = [p for p in with_naps(find_sleep(activity, self.cfg.sleep), naps) if p.end > since]
        return summary, periods, activity, since

    def chart(self) -> str | Photo:
        now = self.status.time or datetime.now()
        summary, periods, _, _ = self._recent(now)
        day_start = parse_day_start(self.cfg.tracking.day_starts_at)
        first = day_bounds(day_of(now, day_start) - timedelta(days=CHART_DAYS - 1), day_start)[0]
        storage = Storage(self.cfg.storage.db_path)
        try:
            samples = storage.samples(first, now)
        finally:
            storage.close()
        days = build_report(
            samples,
            center_threshold=self.cfg.head.center_threshold,
            confirm_seconds=self.cfg.tracking.confirm_seconds,
            gap_seconds=self.cfg.tracking.gap_seconds,
            day_start=day_start,
        )
        png = render_chart(summary, periods, days, t("chart.title_recent"), day_start)
        if png is None:
            return t("report.no_data")
        return Photo(png, t("bot.chart_caption", time=f"{now:%H:%M}"), filename="chart.png")

    def now_text(self) -> str:
        status = self.status
        if status.current is None:
            return t("bot.no_data")
        side = status.current.side
        if side in (Side.LEFT, Side.RIGHT):
            lines = [t("bot.now_side", side=side_label(side), duration=fmt_duration(status.stint))]
        else:
            lines = [t(f"bot.now_{side.value}")]
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
        if side != Side.ABSENT:
            now = status.time or datetime.now()
            asleep, period = sleep_state(self._activity(now - SLEEP_LOOKBACK, now), self.cfg.sleep, now)
            if asleep:
                lines.append(t("bot.now_asleep", duration=fmt_duration(period.seconds), since=f"{period.start:%H:%M}"))
            elif asleep is not None:
                lines.append(t("bot.now_awake"))
        return "\n".join(lines + self.climate_lines())

    def climate_lines(self) -> list[str]:
        thermometers = self.cfg.thermometers
        if not thermometers.devices:
            return []
        storage = Storage(self.cfg.storage.db_path)
        try:
            readings = storage.latest_climate()
        finally:
            storage.close()
        return climate_lines(thermometers.devices, readings, datetime.now(), thermometers.every_minutes,
                             thermometers.low_battery_volts)

    def _activity(self, since: datetime, until: datetime) -> list:
        storage = Storage(self.cfg.storage.db_path)
        try:
            return storage.activity(since, until)
        finally:
            storage.close()

    def sleep_text(self) -> str:
        now = self.status.time or datetime.now()
        _, periods, activity, since = self._recent(now)
        if not periods:
            return no_sleep_text(activity)
        total = sum((p.end - max(p.start, since)).total_seconds() for p in periods)
        lines = [
            t(
                "bot.sleep",
                total=fmt_duration(total),
                count=len(periods),
                longest=fmt_duration(max(p.seconds for p in periods)),
            )
        ]
        return "\n".join(lines + period_lines(periods, now))

    def stats_text(self) -> str:
        now = self.status.time or datetime.now()
        s, periods, _, since = self._recent(now)
        if s is None:
            return t("report.no_data")
        text = t(
            "bot.stats",
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
            total = sum((p.end - max(p.start, since)).total_seconds() for p in periods)
            text += "\n" + t("bot.stats_sleep", total=fmt_duration(total), count=len(periods))
        return text

def load_history(storage: Storage, cfg: Config, until: datetime) -> list[Segment]:
    """Segments from the last window_hours in the database, smoothed as in the report, so that
    after a restart the program remembers how long the head has already been on each side."""
    since = until - timedelta(hours=cfg.alerts.window_hours)
    samples = reclassify(storage.samples(since, until), cfg.head.center_threshold)
    return build_segments(samples, cfg.tracking.confirm_seconds, cfg.tracking.gap_seconds)


def monitor_from_config(cfg: Config) -> BalanceMonitor:
    a = cfg.alerts
    return BalanceMonitor(
        max_same_side=timedelta(minutes=a.max_same_side_minutes),
        max_imbalance=timedelta(minutes=a.max_imbalance_minutes),
        window=timedelta(hours=a.window_hours),
        min_turn=timedelta(minutes=a.min_turn_minutes),
        repeat=timedelta(minutes=a.repeat_minutes),
    )
