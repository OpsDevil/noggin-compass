"""Settings from a YAML file; every option has a default."""

from __future__ import annotations

import json
import os
import re
import shutil
import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass, replace
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
    # Telegram says so when the camera has sent nothing for this many minutes, and again when it
    # is back: the program does not see the crib meanwhile. 0 turns it off.
    silent_minutes: float = 5


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
    # A person smaller than this share of the frame (after camera.roi) is ignored. With the frame
    # cropped to the crib, a baby takes a far larger share than the "people" the network finds in
    # an empty crib (toys, the rail, the sheet print).
    min_person_area: float = 0.06
    min_keypoint_conf: float = 0.4


@dataclass
class PresenceConfig:
    # A second network that looks at the whole frame and says whether the baby is in the crib at
    # all, so that prints on the sheet, toys and clothes laid out are not taken for the baby. Its
    # weights (about 1.5 GB) are downloaded into models/ on the first run.
    enabled: bool = True
    # A fixed-resolution SigLIP or SigLIP 2 model from Hugging Face (not a NaFlex one).
    model: str = "google/siglip2-base-patch16-224"
    # The baby counts as in the crib when the network is at least this sure (0..1). A baby usually
    # scores well above it and an empty crib well below. Each snapshot keeps its score in
    # analysis.jsonl ("presence").
    threshold: float = 0.35
    # How often to look. In between the last answer holds.
    every_seconds: float = 5


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
    # Start of a report day: calendar days, midnight to midnight. A sleep across midnight gives
    # each day its hours, and counts as falling asleep once, in the day it began.
    day_starts_at: str = "00:00"


@dataclass
class AlertsConfig:
    # Alert when the head stays on one side for more than N minutes in a row. 0 disables.
    max_same_side_minutes: float = 0
    # Alert when, within the last window_hours, one side has N minutes more than the other and
    # the head is on it again. 0 disables.
    max_imbalance_minutes: float = 0
    window_hours: float = 12
    # A turn to the other side shorter than this does not end the stint on the first side. Toward
    # morning a newborn turns its head on its own every few minutes, for up to ~10 minutes each
    # time; with a longer limit those turns were ignored and the alert claimed hours on one side.
    min_turn_minutes: float = 10
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
    # A minute is restless when at least this share of its frames had movement. A sleeping baby
    # twitches, and a parent's hand or a light from the door adds more.
    restless_share: float = 0.3
    # Asleep after fall_asleep_minutes still minutes within fall_asleep_window_minutes; the sleep
    # starts at the first of them. Falling asleep in the day, a baby waves its arms with eyes
    # closed, so a short nap may never have the whole window still.
    fall_asleep_minutes: int = 8
    fall_asleep_window_minutes: int = 10
    # Awake after wake_minutes restless minutes within wake_window_minutes. In light morning
    # sleep, with eyes closed, a newborn turns its head every 1-3 minutes and parents fix the
    # blanket, giving 3-4 restless minutes in a row; a baby that is really awake stays restless
    # far longer.
    wake_minutes: int = 7
    wake_window_minutes: int = 10
    # Out of the crib only after this many minutes in a row without the baby. Above 1, a minute or
    # two in which the network lost a still baby (a white sleep sack in daylight) does not end the
    # sleep. 1 by default: in an empty crib the network found "people" in the sheet's pattern, those
    # frames looked still, and joined with the short gaps between them they made up sleep.
    out_minutes: int = 1


@dataclass
class StorageConfig:
    db_path: str = "data/noggin-compass.db"
    snapshots_dir: str = "data/snapshots"
    # Save a frame every N seconds (to check and fine-tune on the real camera). 0 disables.
    snapshot_every_seconds: float = 0


@dataclass
class ClimateLimits:
    """What is too cold, too warm, too dry or too damp for a baby's sleep. The defaults follow
    common pediatric advice: 18-22 °C and 40-60% humidity. Any thermometer can have its own."""

    # Below this the cold alarm starts. 18 °C is the bottom of the range usually given for a
    # baby's room, and the WHO's minimum for a home.
    cold_below: float = 18
    # Above this /now and Telegram suggest dressing the baby lighter or turning the heating down.
    hot_above: float = 22
    # Outside dry_below..humid_above they suggest a humidifier or a dehumidifier.
    dry_below: float = 40
    humid_above: float = 60
    # Whether cold here starts the alarm. Off: a tip like for the heat, no sound.
    alarm: bool = True


LIMITS = tuple(f.name for f in fields(ClimateLimits))


@dataclass
class Thermometer:
    address: str  # as `noggin-compass thermometer` shows it; on a Mac it differs from the sticker
    name: str = ""  # shown when there are several thermometers
    # This thermometer's own limits, by name as in ClimateLimits; the rest come from
    # thermometers.limits.
    limits: dict[str, float | bool] = field(default_factory=dict)

    def limits_from(self, defaults: ClimateLimits) -> ClimateLimits:
        return replace(defaults, **self.limits)


@dataclass
class ColdAlarmConfig:
    # Below cold_below: first a beep on the computer every quiet_every_seconds and one Telegram
    # message. The first reading check_minutes later or after still below cold_below: the alarm, a siren with no pause and a
    # Telegram message with an Off button every repeat_minutes. Meant to wake. At cold_below or
    # above, it all ends.
    check_minutes: float = 10
    repeat_minutes: float = 1
    # After Off the alarm is quiet this long, then the first reading still below cold_below brings it back.
    snooze_minutes: float = 20
    # While it is colder than cold_below + close_degrees, read the thermometers every
    # every_minutes instead of thermometers.every_minutes, so the alarm follows the temperature.
    every_minutes: float = 2
    close_degrees: float = 1.0
    # /mute in Telegram (or the button under a cold message) mutes the cold alarm for this many
    # hours unless told otherwise: for a walk, or while the room is aired. /unmute ends it.
    mute_hours: float = 2
    # How often the beep sounds before the alarm.
    quiet_every_seconds: float = 10
    # The program's own sounds: "beep" (four short beeps) and "siren" (a rising wail and the same
    # beeps), or a macOS system sound (Tink, Sosumi, Funk...) or a sound file. Volume 0..1, of the
    # Mac's own volume, which the program leaves alone: keep it up at night.
    quiet_sound: str = "beep"
    quiet_volume: float = 1.0
    alarm_sound: str = "siren"
    alarm_volume: float = 1.0


@dataclass
class ThermometersConfig:
    # Chosen with `noggin-compass thermometer`; empty means no thermometers are read.
    devices: list[Thermometer] = field(default_factory=list)
    # Each reading connects for about 10 s, which costs the coin cell more than the thermometer's
    # own work, so not too often: the room temperature changes slowly anyway.
    every_minutes: float = 10
    # How long to look for the thermometers before a reading. A thermometer advertises every few
    # seconds, and with a weak signal many are lost, so finding one can take most of a minute.
    # Each is read as soon as it is seen.
    scan_seconds: float = 90
    # Below this /now and Telegram say the battery runs low. A new CR2032 gives about 3.0 V, and
    # the thermometer stops working at about 2.2 V.
    low_battery_volts: float = 2.5
    # Telegram says a thermometer is lost when it has not answered this long.
    lost_minutes: float = 30
    # A tip about the heat or the humidity comes to Telegram, without a sound, when it starts, and
    # again no sooner than this.
    tips_every_hours: float = 3
    limits: ClimateLimits = field(default_factory=ClimateLimits)
    cold: ColdAlarmConfig = field(default_factory=ColdAlarmConfig)


@dataclass
class Config:
    # Interface language: en or ru.
    language: str = "en"
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    head: HeadConfig = field(default_factory=HeadConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    thermometers: ThermometersConfig = field(default_factory=ThermometersConfig)


def _fits(value: Any, hint: Any) -> bool:
    """Whether a value from the YAML suits an option's type. A number in quotes, or "yes" where a
    number goes, would otherwise get through and fail only where the option is first used, which
    for the cold alarm is in the middle of a cold night."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return any(_fits(value, h) for h in typing.get_args(hint))
    if hint is type(None):
        return value is None
    if hint is bool:
        return isinstance(value, bool)
    if hint in (int, float):
        # YAML reads 10 as an int, so a float option takes one too. A bool is an int in Python.
        return not isinstance(value, bool) and isinstance(value, int if hint is int else (int, float))
    if hint is str:
        return isinstance(value, str)
    if origin is list:
        args = typing.get_args(hint)
        return isinstance(value, list) and (not args or all(_fits(v, args[0]) for v in value))
    if origin is dict:
        return isinstance(value, dict)
    return True  # a thermometer and the like: checked where it is built


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    known = {f.name for f in fields(obj)}
    hints = typing.get_type_hints(type(obj))
    for key, value in data.items():
        if key not in known:
            raise ValueError(t("err.unknown_option", key=f"{path}{key}"))
        current = getattr(obj, key)
        if is_dataclass(current):
            if value is None:  # a section with everything in it commented out
                continue
            if not isinstance(value, dict):
                raise ValueError(t("err.not_section", key=f"{path}{key}"))
            _apply(current, value, f"{path}{key}.")
        elif not _fits(value, hints[key]):
            raise ValueError(t("err.bad_value", key=f"{path}{key}", value=value))
        else:
            setattr(obj, key, value)


# Options where 0 or less would stop the program from working, for example read the thermometers
# in a tight loop with no pause.
POSITIVE = (
    "camera.sample_fps",
    "thermometers.every_minutes",
    "thermometers.scan_seconds",
    "thermometers.lost_minutes",
    "thermometers.tips_every_hours",
    "thermometers.cold.check_minutes",
    "thermometers.cold.repeat_minutes",
    "thermometers.cold.snooze_minutes",
    "thermometers.cold.every_minutes",
    "thermometers.cold.quiet_every_seconds",
    "thermometers.cold.mute_hours",
)


def _check(cfg: Config) -> None:
    """Values of the right type that still make no sense: caught at the start, not mid-night."""
    if cfg.camera.rotate not in (0, 90, 180, 270):
        raise ValueError(t("err.rotate"))
    roi = cfg.camera.roi
    if roi is not None and not (len(roi) == 4 and 0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        raise ValueError(t("err.roi"))
    if cfg.head.fallback_head_direction not in ("up", "down", "left", "right"):
        raise ValueError(t("err.fallback_direction"))
    from .report import parse_day_start  # the one parser of it, so what passes here works there

    try:
        parse_day_start(cfg.tracking.day_starts_at)
    except ValueError:
        raise ValueError(t("err.day_start")) from None
    for key in POSITIVE:
        value: Any = cfg
        for part in key.split("."):
            value = getattr(value, part)
        if value <= 0:
            raise ValueError(t("err.positive", key=key))


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


def language_of(path: str | Path) -> str:
    """The language a config file asks for, read before the rest of it is checked, so that what
    is wrong in it is said in that language. "en" when it cannot be read."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "en"
    language = data.get("language") if isinstance(data, dict) else None
    return language if isinstance(language, str) else "en"


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path is not None:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(t("err.not_config", path=path))
        _apply(cfg, data)
    cfg.thermometers.devices = _thermometers(cfg.thermometers.devices)
    _check(cfg)
    return cfg


def _thermometers(items: Any) -> list[Thermometer]:
    if not items:
        return []
    if not isinstance(items, list):
        raise ValueError(t("err.thermometers"))
    devices = []
    for i, item in enumerate(items):
        if isinstance(item, Thermometer):
            devices.append(item)
            continue
        if not isinstance(item, dict) or not isinstance(item.get("address"), str):
            raise ValueError(t("err.thermometers"))
        unknown = set(item) - {"address", "name", *LIMITS}
        if unknown:
            raise ValueError(t("err.unknown_option", key=f"thermometers.devices.{i + 1}.{sorted(unknown)[0]}"))
        limits = {key: item[key] for key in LIMITS if key in item}
        for key, value in limits.items():
            # A bool is an int in Python, so check it first: "alarm: 18" or "cold_below: yes" are typos.
            wrong = not isinstance(value, bool) if key == "alarm" else isinstance(value, bool) or not isinstance(value, (int, float))
            if wrong:
                raise ValueError(t("err.thermometer_value", key=f"thermometers.devices.{i + 1}.{key}"))
        # Bluetooth addresses are written in capitals, by bleak too; "ab:cd" and "AB:CD" are one.
        address = item["address"].strip().upper()
        if any(device.address == address for device in devices):
            raise ValueError(t("err.thermometer_twice", key=f"thermometers.devices.{i + 1}", address=address))
        devices.append(Thermometer(address, str(item.get("name") or "").strip(), limits))
    return devices


def save_thermometers(path: str | Path, devices: list[Thermometer]) -> None:
    """Write thermometers.devices into the config file and leave the rest of it as it is, comments
    included: a YAML library would drop them when writing the whole file back."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    block = ["  devices:" + ("" if devices else " []")]
    for device in devices:
        block.append(f"    - address: {json.dumps(device.address)}")
        if device.name:
            block.append(f"      name: {json.dumps(device.name, ensure_ascii=False)}")
        for key, value in device.limits.items():  # kept when the choice is changed in the menu
            block.append(f"      {key}: {json.dumps(value)}")

    section = next((i for i, line in enumerate(lines) if re.fullmatch(r"thermometers:\s*(#.*)?", line)), None)
    if section is None and any(line.startswith("thermometers:") for line in lines):
        # "thermometers: {...}" on one line: a second section after it would quietly win over it.
        raise ValueError(t("err.thermometers_save", path=path))
    if section is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["thermometers:", *block]
    else:
        end = section + 1  # the section runs to the next line that starts at the left edge
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in " #"):
            end += 1
        start = next((i for i in range(section + 1, end) if lines[i].lstrip().startswith("devices:")), None)
        if start is None:
            lines[section + 1:section + 1] = block
        else:
            indent = len(lines[start]) - len(lines[start].lstrip())
            stop = start + 1  # the old list: the lines indented deeper than "devices:"
            while stop < end and lines[stop].strip() and len(lines[stop]) - len(lines[stop].lstrip()) > indent:
                stop += 1
            lines[start:stop] = block
    text = "\n".join(lines) + "\n"
    saved = _thermometers((yaml.safe_load(text) or {}).get("thermometers", {}).get("devices"))
    if saved != devices:  # the file had an unusual layout: better to say so than to write it wrong
        raise ValueError(t("err.thermometers_save", path=path))
    # Through a temporary file: the config holds the camera password and the bot token, and a
    # crash in the middle of writing must not leave half of it.
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, temporary)
    os.replace(temporary, path)
