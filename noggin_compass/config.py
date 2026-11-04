"""Settings from a YAML file; every option has a default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .i18n import t


@dataclass
class CameraConfig:
    # RTSP URL, local camera index (0 is the Mac's built-in camera) or path to a video file.
    source: str | int = 0
    # Frames analysed per second. The head does not turn often, so 1 is enough.
    sample_fps: float = 1.0
    # Rotate the frame before analysis (0/90/180/270 clockwise), ideally so the baby's head is up.
    rotate: int = 0
    # Crib area as fractions of the frame [x1, y1, x2, y2]; the rest is ignored. None: whole frame.
    roi: list[float] | None = None
    # Camera password for {password} in source. NOGGIN_CAMERA_PASSWORD takes precedence.
    password: str | None = None


@dataclass
class DetectorConfig:
    model: str = "models/yolo11s-pose.pt"
    # cpu is the most reliable; mps is worth a try on Apple Silicon.
    device: str = "cpu"
    # Model input size. Larger is more accurate on a small head, but slower.
    imgsz: int = 640
    # Second pass on a crop rotated so the baby's head is up. Twice as slow, but face points are
    # found much more reliably when the baby lies across the frame.
    upright: bool = True
    min_person_conf: float = 0.35
    min_keypoint_conf: float = 0.4


@dataclass
class HeadConfig:
    # |score| below this counts as straight. score ~ 0.9 * tan(angle), so 0.35 is ~20 degrees.
    center_threshold: float = 0.35
    # True if the camera image is mirrored (flip/mirror on). Swaps left and right.
    mirrored: bool = False
    # Where the top of the head points in the frame if the shoulders are hidden: up/down/left/right.
    fallback_head_direction: str = "up"


@dataclass
class TrackingConfig:
    # A new position counts only after it has held this long, in seconds (filters out jitter).
    confirm_seconds: float = 10.0
    # A gap in the data longer than this (camera dropped out) is not counted.
    gap_seconds: float = 60.0
    # Start of a report day. Noon to noon keeps a whole night in one day.
    day_starts_at: str = "12:00"


@dataclass
class AlertsConfig:
    # Alert when the head stays on one side for more than N minutes in a row. 0 disables.
    max_same_side_minutes: float = 0
    # Alert when, within the last window_hours, one side has N minutes more than the other and
    # the head is on it again. 0 disables.
    max_imbalance_minutes: float = 0
    window_hours: float = 12
    # A turn to the other side shorter than this does not end the stint on the first side.
    min_turn_minutes: float = 15
    # Repeat interval of the alert while the head stays on the overloaded side.
    repeat_minutes: float = 15
    # Play a sound on the computer running the program (macOS only).
    sound: bool = True
    # macOS system sound name (Funk, Glass, Sosumi, Basso, etc.) or a path to a sound file.
    sound_file: str = "Funk"


@dataclass
class TelegramConfig:
    # Bot token from @BotFather. NOGGIN_TELEGRAM_TOKEN takes precedence.
    token: str | None = None
    # Chats that get alerts and may send commands. NOGGIN_TELEGRAM_CHAT_IDS (comma-separated)
    # takes precedence.
    chat_ids: list[int] = field(default_factory=list)
    # Allow /photo and /marked. Off by default: pictures of the baby would go through Telegram.
    photos: bool = False


@dataclass
class SleepConfig:
    # A frame counts as movement when this share of the baby's area changed since the previous one.
    frame_motion: float = 0.02
    # A minute is restless when at least this share of its frames had movement.
    restless_share: float = 0.1
    # Asleep after this many still minutes in a row; the sleep starts at the first of them.
    fall_asleep_minutes: int = 10
    # Awake after wake_minutes restless minutes within wake_window_minutes.
    wake_minutes: int = 3
    wake_window_minutes: int = 5


@dataclass
class StorageConfig:
    db_path: str = "data/noggin-compass.db"
    snapshots_dir: str = "data/snapshots"
    # Save a frame every N seconds (to check and fine-tune on the real camera). 0 disables.
    snapshot_every_seconds: float = 0


@dataclass
class Config:
    # Interface language: en or ru.
    language: str = "en"
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    head: HeadConfig = field(default_factory=HeadConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(t("err.unknown_option", key=f"{path}{key}"))
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(t("err.not_section", key=f"{path}{key}"))
            _apply(current, value, f"{path}{key}.")
        else:
            setattr(obj, key, value)


def load_dotenv(path: str | Path) -> None:
    """Load KEY=VALUE lines from .env into the environment. Variables that are already set win,
    empty values are skipped. Only whole-line comments are supported: a password may contain '#'."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value:
            os.environ.setdefault(key, value)


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path is not None:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        _apply(cfg, data)
    return cfg
