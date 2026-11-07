"""Bugs found in review, each pinned down so it cannot come back."""

import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from noggin_compass import keychain, notify, telegram
from noggin_compass.cli import load_snapshot, save_snapshot
from noggin_compass.config import Config, DetectorConfig, SleepConfig, Thermometer, load_config, save_thermometers
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.preview import Frame
from noggin_compass.sleep import find_sleep, within, SleepPeriod
from noggin_compass.source import LiveSource
from noggin_compass.storage import Storage
from noggin_compass.tracker import Segment

T0 = datetime(2026, 11, 6, 22, 0)


@pytest.fixture(autouse=True)
def no_real_bot(monkeypatch):
    monkeypatch.delenv(telegram.TOKEN_ENV, raising=False)
    monkeypatch.delenv(telegram.CHAT_IDS_ENV, raising=False)
    monkeypatch.setattr(keychain, "read", lambda service: None)


# --- The bot -------------------------------------------------------------------------------------

def test_a_failing_command_does_not_block_the_ones_after_it():
    sent, handled = [], []

    def handler(text):
        handled.append(text)
        if text == "/chart":
            raise RuntimeError("matplotlib broke")
        return "ok " + text

    bot = telegram.TelegramBot("token", [10], handler)
    bot.send = lambda text, chat_ids=None, silent=False: sent.append((text, chat_ids))
    updates = [{"update_id": n, "message": {"chat": {"id": 10}, "text": text}}
               for n, text in ((1, "/now"), (2, "/chart"), (3, "/sleep"))]
    assert bot.handle_updates(updates) == 4  # past all three: none of them comes back
    assert handled == ["/now", "/chart", "/sleep"]
    assert sent[0] == ("ok /now", [10]) and sent[2] == ("ok /sleep", [10])
    assert "went wrong" in sent[1][0]  # the parent hears of it


def test_a_picture_that_cannot_be_sent_gets_a_word():
    sent = []
    bot = telegram.TelegramBot("token", [10], lambda text: telegram.Photo(b"\xff\xd8", "caption"))
    bot._post = lambda *args: (_ for _ in ()).throw(OSError("no network"))
    bot.send = lambda text, chat_ids=None, silent=False: sent.append(text)
    bot.handle_updates([{"update_id": 1, "message": {"chat": {"id": 10}, "text": "/photo"}}])
    assert sent == ["Could not send the picture. Try again."]


def test_help_offers_only_the_commands_that_are_on():
    cfg = Config()
    off = notify.Notifier(cfg).help_text()
    assert "/photo" not in off and "/marked" not in off and "/stats - time per side over the last 24 hours" in off
    cfg.telegram.photos = True
    assert "/photo - a picture from the camera right now" in notify.Notifier(cfg).help_text()


def test_now_says_when_the_camera_is_silent(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    notifier = notify.Notifier(cfg)
    current = Segment(Side.LEFT, T0, T0 + timedelta(minutes=30))
    notifier.status = notify.Status(time=current.end, current=current, stint=1800.0)
    notifier.clock = lambda: current.end + timedelta(seconds=30)
    assert "camera" not in notifier.now_text()
    notifier.clock = lambda: current.end + timedelta(hours=2)
    assert notifier.now_text().startswith("No frames from the camera since 22:30.")


def test_a_sleep_that_began_before_the_window_counts_only_its_part_inside():
    since = T0
    periods = [SleepPeriod(T0 - timedelta(hours=2), T0 + timedelta(hours=1))]
    inside = within(periods, since)
    assert inside[0].start == T0 and inside[0].seconds == 3600
    assert within([SleepPeriod(T0 - timedelta(hours=3), T0 - timedelta(hours=1))], since) == []


def test_messages_wait_for_nobody_once_the_outbox_runs():
    gate, done = threading.Event(), threading.Event()

    def slow(text):
        gate.wait(2)  # a Telegram that hangs
        done.set()

    outbox = notify.Outbox()
    outbox.start()
    began = time.monotonic()
    outbox.put(slow, "alarm")
    assert time.monotonic() - began < 0.5  # the caller (camera loop, siren) goes on
    gate.set()
    assert done.wait(2)
    outbox.stop()


# --- The camera ----------------------------------------------------------------------------------

def test_a_frame_is_handed_out_once_even_while_the_camera_stalls():
    source = LiveSource.__new__(LiveSource)  # without its reader thread
    source._lock = threading.Lock()
    source._running = True
    source._frame, source._frame_time = np.zeros((4, 4, 3), np.uint8), time.time()
    got = []

    def collect():
        for ts, _ in source.frames(sample_fps=50):
            got.append(ts)

    reader = threading.Thread(target=collect)
    reader.start()
    time.sleep(0.3)  # 15 ticks with no new frame
    with source._lock:
        source._frame_time = time.time()
    time.sleep(0.2)
    source._running = False
    reader.join(1)
    assert len(got) == 2 and got[0] < got[1]


# --- The config ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, key", [
    ("thermometers:\n  limits:\n    cold_below: '18'\n", "thermometers.limits.cold_below"),
    ("thermometers:\n  limits:\n    alarm: 'false'\n", "thermometers.limits.alarm"),
    ("thermometers:\n  cold:\n    check_minutes: ten\n", "thermometers.cold.check_minutes"),
    ("alerts:\n  sound: 1\n", "alerts.sound"),
    ("thermometers:\n  every_minutes: 0\n", "thermometers.every_minutes"),
    ("camera:\n  roi: [0.8, 0.2, 0.3, 0.9]\n", "camera.roi"),
    ("camera:\n  rotate: 45\n", "camera.rotate"),
    ("tracking:\n  day_starts_at: noon\n", "tracking.day_starts_at"),
    ("head:\n  fallback_head_direction: sideways\n", "head.fallback_head_direction"),
])
def test_a_wrong_value_is_caught_when_the_config_loads(tmp_path, text, key):
    config = tmp_path / "config.yaml"
    config.write_text(text)
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        load_config(config)


def test_what_the_config_may_hold(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("thermometers:\n  # everything here commented out\ncamera:\n  source: rtsp://cam/stream\n")
    assert load_config(config).camera.source == "rtsp://cam/stream"  # an empty section is fine
    config.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="name: value"):
        load_config(config)
    address = "5e1a7c30-9b2d-4f86-a1c4-3d7e9f2b6a18"
    config.write_text(f"thermometers:\n  devices:\n    - address: {address}\n")
    assert load_config(config).thermometers.devices[0].address == address.upper()
    config.write_text(f"thermometers:\n  devices:\n    - address: {address}\n    - address: {address.upper()}\n")
    with pytest.raises(ValueError, match="already in the list"):
        load_config(config)


def test_saving_the_choice_keeps_the_files_permissions(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("telegram:\n  token: secret\n")
    os.chmod(config, 0o600)
    save_thermometers(config, [Thermometer("5E1A7C30-9B2D-4F86-A1C4-3D7E9F2B6A18")])
    assert stat.S_IMODE(config.stat().st_mode) == 0o600 and "token: secret" in config.read_text()
    assert not (tmp_path / "config.yaml.tmp").exists()


# --- Sleep and naps ------------------------------------------------------------------------------

def test_a_wake_is_not_swallowed_by_the_still_minutes_before_it():
    cfg = SleepConfig(fall_asleep_minutes=5, fall_asleep_window_minutes=10, wake_minutes=3, wake_window_minutes=10)
    samples = []
    for minute in range(25):
        restless = minute in (10, 11, 12)
        for second in range(0, 60, 10):
            samples.append((T0 + timedelta(minutes=minute, seconds=second), Side.LEFT, 0.5 if restless else 0.0))
    first, second = find_sleep(samples, cfg)
    assert first.end == T0 + timedelta(minutes=10)
    assert second.start == T0 + timedelta(minutes=13)  # after the wake, not before it


def test_a_nap_button_pressed_twice_logs_one_nap(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    one = storage.add_nap(T0, T0 + timedelta(minutes=30))
    assert storage.add_nap(T0, T0 + timedelta(minutes=30)) == one
    assert len(storage.naps()) == 1
    storage.close()


# --- Snapshots -----------------------------------------------------------------------------------

def test_draw_takes_the_last_record_of_a_file_and_skips_a_broken_line(tmp_path):
    image = np.zeros((40, 60, 3), np.uint8)
    ts = datetime(2026, 11, 6, 8, 0, 0)
    save_snapshot(Frame(image, None, HeadEstimate(Side.ABSENT, method="no_person"), ts, ["first"]),
                  str(tmp_path), DetectorConfig())
    save_snapshot(Frame(image, None, HeadEstimate(Side.ABSENT, method="no_person"), ts, ["again"]),
                  str(tmp_path), DetectorConfig())
    analysis = tmp_path / "2026-11-06" / "analysis.jsonl"
    with open(analysis, "a", encoding="utf-8") as out:
        out.write('{"file": "080000_absent_na.jpg", "si')  # the program stopped mid-line
    assert load_snapshot(tmp_path / "2026-11-06" / "080000_absent_na.jpg", Config()).notes == ["again"]
    assert len(analysis.read_text().splitlines()) == 3 and json.loads(analysis.read_text().splitlines()[0])


# --- The camera going silent, a recorded video -----------------------------------------------------

def test_telegram_hears_once_when_the_camera_goes_silent_and_once_when_it_is_back(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    notifier = notify.Notifier(cfg)
    sent, notes = [], []
    notifier.send = lambda text, silent=False: sent.append((text, silent))
    notifier.notify = notes.append
    current = Segment(Side.LEFT, T0, T0 + timedelta(minutes=10))
    notifier.status = notify.Status(time=current.end, current=current)
    notifier.clock = lambda: current.end + timedelta(minutes=4)
    notifier.check_camera()
    assert sent == []  # 4 minutes: not yet
    notifier.clock = lambda: current.end + timedelta(minutes=6)
    notifier.check_camera()
    notifier.check_camera()
    assert sent == [("The camera has sent nothing since 22:10: the program does not see the crib. "
                     "Check the camera and the Wi-Fi.", False)] and len(notes) == 1

    class Monitor:
        def stint(self, now, current): return 0.0
        def totals(self, now, current): return {}
        def due(self, now, current): return None

    notifier.update(current.end + timedelta(minutes=7), current, Monitor())
    notifier.update(current.end + timedelta(minutes=7, seconds=1), current, Monitor())
    assert sent[1:] == [("The camera works again.", True)]


def test_a_recorded_video_sends_nothing(monkeypatch):
    monkeypatch.setenv(telegram.TOKEN_ENV, "token")
    monkeypatch.setenv(telegram.CHAT_IDS_ENV, "10")
    assert notify.Notifier(Config()).bot is not None
    quiet = notify.Notifier(Config(), live=False)
    assert quiet.bot is None
    told = []
    quiet.notify = told.append
    from noggin_compass.balance import Alert
    quiet.alert(Alert("stint", Side.LEFT, Side.RIGHT, 3600.0, {Side.LEFT: 3600.0, Side.RIGHT: 0.0}))
    assert told == []  # in the log only


def test_a_side_is_not_guessed_from_face_points_the_second_pass_could_not_check():
    from noggin_compass.cli import analyze
    from noggin_compass.pose import Person

    xy, conf = np.zeros((17, 2)), np.zeros(17)
    xy[[0, 1, 2, 4, 5, 6]] = [(200, 100), (190, 95), (210, 95), (230, 100), (170, 160), (230, 160)]
    conf[[0, 1, 2, 4, 5, 6]] = 0.9

    class Detector:
        def __init__(self, checked):
            self.checked = checked

        def detect_upright(self, frame, min_conf, now):
            return Person(np.array([150.0, 80.0, 250.0, 280.0]), 0.9, xy, conf, checked=self.checked)

    cfg = Config()
    frame = np.zeros((300, 400, 3), np.uint8)
    checked, _ = analyze(Detector(True), frame, cfg, 0.0)
    assert checked.side != Side.UNKNOWN and checked.code.startswith("ear_and_eyes")
    estimate, person = analyze(Detector(False), frame, cfg, 0.0)
    assert estimate.side == Side.UNKNOWN and estimate.code == "unchecked" and person is not None



def test_mute_and_unmute_from_telegram(tmp_path):
    from noggin_compass.climate import ClimateWatch

    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    assert "/mute" not in notify.Notifier(cfg).help_text()  # no thermometers: nothing to mute
    assert notify.Notifier(cfg).handle_command("/mute") == "No thermometers are chosen, so there is no cold alarm to mute."
    cfg.thermometers.devices = [Thermometer("5E1A7C30-9B2D-4F86-A1C4-3D7E9F2B6A18", "Crib")]
    notifier = notify.Notifier(cfg)
    help_text = notifier.help_text()
    assert "/mute - mute the cold alarm for a while, 2 h unless you give the hours" in help_text
    assert "/unmute - turn the cold alarm back on" in help_text
    now = datetime(2026, 11, 6, 14, 0)
    notifier.climate = ClimateWatch(cfg.thermometers, clock=lambda: now)
    assert notifier.handle_command("/mute").text.startswith("The cold alarm is muted until 16:00")
    assert notifier.handle_command("/mute 1,5").text.startswith("The cold alarm is muted until 15:30")
    assert notifier.handle_command("/mute later") == "Give the hours as a number from 0 to 24, for example /mute 3."
    assert notifier.handle_command("/mute 30") == "Give the hours as a number from 0 to 24, for example /mute 3."
    assert notifier.press("climate:mute:4").text.startswith("The cold alarm is muted until 18:00")
    assert notifier.handle_command("/unmute").text == "The cold alarm is on."
