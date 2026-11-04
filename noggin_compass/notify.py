"""The "time to turn the baby" alert: a sound and a notification on the Mac, a Telegram message.
Also the bot's replies to /now and /stats."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .balance import Alert, BalanceMonitor
from .config import Config
from .head import Side, side_label
from .i18n import t
from .report import build_report, day_bounds, day_of, fmt_duration, parse_day_start, reclassify
from .storage import Storage
from .telegram import TelegramBot, resolve_credentials
from .tracker import Segment, build_segments

log = logging.getLogger(__name__)

SYSTEM_SOUNDS = Path("/System/Library/Sounds")
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


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.status = Status()
        token, chat_ids = resolve_credentials(cfg.telegram.token, cfg.telegram.chat_ids)
        self.bot = TelegramBot(token, chat_ids, self.handle_command) if token else None

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

    def alert(self, alert: Alert) -> None:
        text = format_alert(alert, self.cfg.alerts.window_hours)
        log.warning(text)
        mac_notification(text)
        if self.cfg.alerts.sound:
            play_sound(self.cfg.alerts.sound_file)
        if self.bot is not None:
            self.bot.send(text)

    def handle_command(self, text: str) -> str:
        command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
        if command == "/now":
            return self.now_text()
        if command == "/stats":
            return self.stats_text()
        return t("bot.help")

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
        return "\n".join(lines)

    def stats_text(self) -> str:
        day_start = parse_day_start(self.cfg.tracking.day_starts_at)
        now = self.status.time or datetime.now()
        since, until = day_bounds(day_of(now, day_start), day_start)
        storage = Storage(self.cfg.storage.db_path)
        try:
            samples = storage.samples(since, until)
        finally:
            storage.close()
        summaries = build_report(
            samples,
            center_threshold=self.cfg.head.center_threshold,
            confirm_seconds=self.cfg.tracking.confirm_seconds,
            gap_seconds=self.cfg.tracking.gap_seconds,
            day_start=day_start,
        )
        if not summaries:
            return t("report.no_data")
        s = summaries[-1]
        return t(
            "bot.stats",
            start=f"{since:%d.%m %H:%M}",
            left=fmt_duration(s.totals[Side.LEFT]),
            left_pct=f"{s.share(Side.LEFT):.0%}",
            right=fmt_duration(s.totals[Side.RIGHT]),
            right_pct=f"{s.share(Side.RIGHT):.0%}",
            center=fmt_duration(s.totals[Side.CENTER]),
            center_pct=f"{s.share(Side.CENTER):.0%}",
            unknown=fmt_duration(s.totals[Side.UNKNOWN]),
            turns=s.turns,
        )


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
