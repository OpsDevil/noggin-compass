import json
from datetime import datetime

import numpy as np

from noggin_compass.cli import save_snapshot
from noggin_compass.config import DetectorConfig
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.pose import Person


def test_snapshot_saves_images_and_analysis(tmp_path):
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    xy = np.arange(34, dtype=float).reshape(17, 2)
    person = Person(np.array([1.0, 2.0, 100.0, 110.0]), 0.9, xy, np.full(17, 0.5))
    ts = datetime(2026, 11, 4, 21, 30, 5)
    estimate = HeadEstimate(Side.LEFT, 0.8123, method="both_ears", axis_source="shoulders")

    save_snapshot(frame, frame, ts, estimate, person, str(tmp_path), DetectorConfig())
    save_snapshot(frame, frame, ts.replace(second=15), HeadEstimate(Side.ABSENT, method="no_person"),
                  None, str(tmp_path), DetectorConfig())

    folder = tmp_path / "2026-11-04"
    assert (folder / "213005_left_+0.81.jpg").exists()
    assert (folder / "213005_left_+0.81_debug.jpg").exists()
    first, second = [json.loads(line) for line in (folder / "analysis.jsonl").read_text().splitlines()]
    assert first["file"] == "213005_left_+0.81.jpg"
    assert first["side"] == "left" and first["score"] == 0.812
    assert first["method"] == "both_ears/shoulders"
    assert len(first["keypoints"]) == 17 and first["keypoints"][1] == [2.0, 3.0, 0.5]
    assert second["side"] == "absent" and second["keypoints"] is None and second["score"] is None
