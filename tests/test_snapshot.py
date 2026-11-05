import json
from datetime import datetime

import cv2
import numpy as np
import pytest

from noggin_compass.cli import load_snapshot, main, save_snapshot
from noggin_compass.config import Config, DetectorConfig
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.pose import Person


def test_snapshot_saves_the_frame_and_analysis(tmp_path):
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    xy = np.arange(34, dtype=float).reshape(17, 2)
    person = Person(np.array([1.0, 2.0, 100.0, 110.0]), 0.9, xy, np.full(17, 0.5))
    ts = datetime(2026, 11, 4, 21, 30, 5)
    estimate = HeadEstimate(Side.LEFT, 0.8123, method="both_ears", axis_source="shoulders")

    save_snapshot(frame, ts, estimate, person, str(tmp_path), DetectorConfig(), presence=0.8761)
    save_snapshot(frame, ts.replace(second=15), HeadEstimate(Side.ABSENT, method="no_person"),
                  None, str(tmp_path), DetectorConfig())

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
    save_snapshot(frame, ts, estimate, person, str(tmp_path), DetectorConfig())
    save_snapshot(frame, ts.replace(second=10), HeadEstimate(Side.ABSENT, method="no_person"),
                  None, str(tmp_path), DetectorConfig())
    folder = tmp_path / "2026-11-05"

    loaded, again, found = load_snapshot(folder / "081200_center_+0.00.jpg", Config())
    assert loaded.shape == frame.shape
    assert again.side == Side.CENTER and again.code == "both_ears/body"
    assert found.box.tolist() == [150.0, 80.0, 250.0, 280.0] and found.conf[0] == 0.9
    assert again.axis == pytest.approx((1.0, 0.0))  # as the live run had it, not recomputed
    _, empty, nobody = load_snapshot(folder / "081210_absent_na.jpg", Config())
    assert empty.side == Side.ABSENT and nobody is None

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
    save_snapshot(frame, datetime(2026, 11, 5, 8, 0), HeadEstimate(Side.CENTER, 0.0, method="both_ears"),
                  person, str(tmp_path), DetectorConfig())
    analysis = tmp_path / "2026-11-05" / "analysis.jsonl"
    record = json.loads(analysis.read_text())
    del record["axis"]
    analysis.write_text(json.dumps(record) + "\n")
    _, estimate, _ = load_snapshot(tmp_path / "2026-11-05" / record["file"], Config())
    assert estimate.axis == pytest.approx((0.0, -1.0))  # shoulders below the face: head up


def test_draw_says_when_the_jsonl_has_no_line(tmp_path):
    folder = tmp_path / "2026-11-05"
    folder.mkdir()
    cv2.imwrite(str(folder / "stray.jpg"), np.zeros((10, 10, 3), dtype=np.uint8))
    with pytest.raises(SystemExit):
        load_snapshot(folder / "stray.jpg", Config())
