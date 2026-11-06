import json
from datetime import datetime

import cv2
import numpy as np
import pytest

from noggin_compass.cli import load_snapshot, main, save_snapshot
from noggin_compass.config import Config, DetectorConfig
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.pose import Person
from noggin_compass.preview import Frame, picture


def test_snapshot_saves_the_frame_and_analysis(tmp_path):
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    xy = np.arange(34, dtype=float).reshape(17, 2)
    person = Person(np.array([1.0, 2.0, 100.0, 110.0]), 0.9, xy, np.full(17, 0.5))
    ts = datetime(2026, 11, 4, 21, 30, 5)
    estimate = HeadEstimate(Side.LEFT, 0.8123, method="both_ears", axis_source="shoulders")

    save_snapshot(Frame(frame, person, estimate, ts, ["Asleep for 40 min, since 20:50."]), str(tmp_path),
                  DetectorConfig(), presence=0.8761)
    save_snapshot(Frame(frame, None, HeadEstimate(Side.ABSENT, method="no_person"), ts.replace(second=15)),
                  str(tmp_path), DetectorConfig())

    folder = tmp_path / "2026-11-04"
    assert (folder / "213005_left_+0.81.jpg").exists()
    # A drawn copy is not kept: `draw` makes one from the jsonl when needed.
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["213005_left_+0.81.jpg", "213015_absent_na.jpg", "analysis.jsonl"]
    first, second = [json.loads(line) for line in (folder / "analysis.jsonl").read_text().splitlines()]
    assert first["file"] == "213005_left_+0.81.jpg"
    assert first["side"] == "left" and first["score"] == 0.812
    assert first["method"] == "both_ears/shoulders"
    assert first["presence"] == 0.876
    assert first["notes"] == ["Asleep for 40 min, since 20:50."] and second["notes"] == []
    assert len(first["keypoints"]) == 17 and first["keypoints"][1] == [2.0, 3.0, 0.5]
    assert second["side"] == "absent" and second["keypoints"] is None and second["score"] is None


def test_drawn_copy_is_made_from_the_frame_and_the_jsonl(tmp_path):
    frame = np.full((300, 400, 3), 90, dtype=np.uint8)
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[:7] = [(200, 100), (190, 95), (210, 95), (180, 100), (220, 100), (170, 160), (230, 160)]
    conf[:7] = 0.9
    person = Person(np.array([150.0, 80.0, 250.0, 280.0]), 0.9, xy, conf)
    estimate = HeadEstimate(Side.CENTER, 0.0, method="both_ears", axis_source="body", axis=(1.0, 0.0))
    ts = datetime(2026, 11, 5, 8, 12, 0)
    notes = ["Asleep for 1 h 05 min, since 07:07.", "Crib: 22.4 °C, humidity 46%."]
    live = Frame(frame, person, estimate, ts, notes)
    save_snapshot(live, str(tmp_path), DetectorConfig())
    save_snapshot(Frame(frame, None, HeadEstimate(Side.ABSENT, method="no_person"), ts.replace(second=10)),
                  str(tmp_path), DetectorConfig())
    folder = tmp_path / "2026-11-05"

    loaded = load_snapshot(folder / "081200_center_+0.00.jpg", Config())
    assert loaded.image.shape == frame.shape and loaded.time == ts and loaded.notes == notes
    assert loaded.estimate.side == Side.CENTER and loaded.estimate.code == "both_ears/body"
    assert loaded.person.box.tolist() == [150.0, 80.0, 250.0, 280.0] and loaded.person.conf[0] == 0.9
    assert loaded.estimate.axis == pytest.approx((1.0, 0.0))  # as the live run had it, not recomputed
    empty = load_snapshot(folder / "081210_absent_na.jpg", Config())
    assert empty.estimate.side == Side.ABSENT and empty.person is None
    # The drawn snapshot is the picture the preview showed then, corner notes included.
    assert np.abs(picture(loaded, Config()).astype(int) - picture(live, Config()).astype(int)).max() == 0

    out = tmp_path / "drawn"
    config = tmp_path / "config.yaml"
    config.write_text("language: en\n")
    main(["-c", str(config), "draw", str(folder / "081200_center_+0.00.jpg"),
          str(folder / "081210_absent_na.jpg"), "-o", str(out)])
    names = sorted(p.name for p in out.iterdir())
    assert names == ["081200_center_+0.00_drawn.jpg", "081210_absent_na_drawn.jpg"]


def test_old_snapshots_without_the_axis_get_it_recomputed(tmp_path):
    frame = np.zeros((300, 400, 3), dtype=np.uint8)
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[:7] = [(200, 100), (190, 95), (210, 95), (180, 100), (220, 100), (170, 160), (230, 160)]
    conf[:7] = 0.9
    person = Person(np.array([150.0, 80.0, 250.0, 280.0]), 0.9, xy, conf)
    save_snapshot(Frame(frame, person, HeadEstimate(Side.CENTER, 0.0, method="both_ears"), datetime(2026, 11, 5, 8, 0)),
                  str(tmp_path), DetectorConfig())
    analysis = tmp_path / "2026-11-05" / "analysis.jsonl"
    record = json.loads(analysis.read_text())
    del record["axis"], record["notes"]  # as snapshots were saved before
    analysis.write_text(json.dumps(record) + "\n")
    old = load_snapshot(tmp_path / "2026-11-05" / record["file"], Config())
    assert old.estimate.axis == pytest.approx((0.0, -1.0))  # shoulders below the face: head up
    assert old.notes == []


def test_draw_says_when_the_jsonl_has_no_line(tmp_path):
    folder = tmp_path / "2026-11-05"
    folder.mkdir()
    cv2.imwrite(str(folder / "stray.jpg"), np.zeros((10, 10, 3), dtype=np.uint8))
    with pytest.raises(SystemExit):
        load_snapshot(folder / "stray.jpg", Config())
