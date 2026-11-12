import sqlite3
from datetime import datetime

import numpy as np
import pytest

from noggin_compass.head import HeadEstimate, Side
from noggin_compass.config import MotionConfig
from noggin_compass.motion import MotionMeter
from noggin_compass.storage import Storage

BOX = np.array([200.0, 100.0, 440.0, 380.0])
CFG = MotionConfig()


def scene(brightness: int = 100) -> np.ndarray:
    frame = np.full((480, 640, 3), brightness, np.uint8)
    frame[150:330, 250:390] = (brightness + 80, brightness + 70, brightness + 60)  # the baby
    return frame


def test_still_baby_does_not_move():
    meter = MotionMeter(CFG)
    assert meter.update(scene(), BOX, 0.0) is None  # nothing to compare with yet
    assert meter.update(scene(), BOX, 1.0) == 0.0


def test_moving_arm_counts():
    meter = MotionMeter(CFG)
    meter.update(scene(), BOX, 0.0)
    moved = scene()
    moved[200:260, 390:430] = 250  # an arm thrown out to the side
    assert meter.update(moved, BOX, 1.0) > 0.02


def test_pixel_threshold_decides_what_counts_as_movement():
    moved = scene()
    moved[200:260, 390:430] = 130  # a faint change next to the baby
    for threshold, moves in ((12, True), (60, False)):
        meter = MotionMeter(MotionConfig(pixel_threshold=threshold))
        meter.update(scene(), BOX, 0.0)
        assert (meter.update(moved, BOX, 1.0) > 0) is moves


def test_frames_further_apart_than_the_gap_are_compared_when_the_gap_is_raised():
    meter = MotionMeter(MotionConfig(max_gap_seconds=30))
    meter.update(scene(), BOX, 0.0)
    assert meter.update(scene(), BOX, 20.0) == 0.0


def test_whole_picture_getting_brighter_is_not_movement():
    meter = MotionMeter(CFG)
    meter.update(scene(100), BOX, 0.0)
    assert meter.update(scene(130), BOX, 1.0) == pytest.approx(0.0, abs=0.01)


def test_no_baby_or_old_frame_gives_none():
    meter = MotionMeter(CFG)
    meter.update(scene(), BOX, 0.0)
    assert meter.update(scene(), None, 1.0) is None
    assert meter.update(scene(), BOX, 2.0 + CFG.max_gap_seconds) is None


def test_storage_adds_motion_column_to_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples (ts REAL NOT NULL, side TEXT NOT NULL, score REAL, method TEXT)")
    conn.execute("INSERT INTO samples VALUES (1000.0, 'left', 0.8, 'both_ears/shoulders')")
    conn.commit()
    conn.close()

    storage = Storage(path)
    storage.add(datetime.fromtimestamp(2000.0), HeadEstimate(Side.RIGHT, -0.9), motion=0.05)
    storage.flush()
    assert storage.activity() == [
        (datetime.fromtimestamp(1000.0), Side.LEFT, None),
        (datetime.fromtimestamp(2000.0), Side.RIGHT, 0.05),
    ]
    assert len(storage.samples()) == 2
    storage.close()
