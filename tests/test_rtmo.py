from pathlib import Path

import numpy as np
import pytest

from noggin_compass import rtmo
from noggin_compass.rtmo import Rtmo, _nms, is_rtmo, weights


def test_rtmo_is_told_by_the_name():
    assert is_rtmo("rtmo-m")
    assert is_rtmo("models/rtmo-l.onnx")
    assert not is_rtmo("models/yolo11s-pose.pt")
    assert not is_rtmo("yolo26s-pose.onnx")


def test_a_name_is_kept_in_the_models_folder_and_downloaded_once(tmp_path, monkeypatch):
    fetched = []

    def download(archive, to):
        fetched.append(archive)
        to.write_bytes(b"onnx")

    monkeypatch.setattr(rtmo, "download", download)
    assert weights("rtmo-m", tmp_path) == tmp_path / "rtmo-m.onnx"
    assert weights("rtmo-m", tmp_path) == tmp_path / "rtmo-m.onnx"
    assert fetched == [rtmo.ARCHIVES["rtmo-m"]]
    # A path is taken as it is, with or without .onnx.
    assert weights(str(tmp_path / "rtmo-m.onnx"), Path("elsewhere")) == tmp_path / "rtmo-m.onnx"
    assert weights(str(tmp_path / "rtmo-m"), Path("elsewhere")) == tmp_path / "rtmo-m.onnx"


def test_an_unknown_rtmo_name_says_which_ones_there_are(tmp_path):
    with pytest.raises(ValueError, match="rtmo-s, rtmo-m, rtmo-l"):
        weights("rtmo-xl", tmp_path)


def test_overlapping_people_are_one_and_the_surer_stays():
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105], [200, 200, 300, 300], [0, 0, 50, 50]], float)
    scores = np.array([0.6, 0.9, 0.5, 0.05])
    assert _nms(boxes, scores, 0.1) == [1, 2]
    assert _nms(boxes, scores, 0.95) == []


class _Session:
    """An ONNX session that returns the given people in the network's 640x640 picture."""

    def __init__(self, people, points):
        self.outputs = [np.asarray(people, float)[None], np.asarray(points, float)[None]]
        self.seen = None

    def get_inputs(self):
        return [type("Input", (), {"name": "input"})()]

    def run(self, names, feed):
        self.seen = feed["input"]
        return self.outputs


def network(people, points) -> Rtmo:
    net = object.__new__(Rtmo)
    net.session, net.input = _Session(people, points), "input"
    return net


def test_people_come_back_in_frame_pixels_with_a_box_around_their_points():
    points = np.zeros((2, 17, 3))
    points[0, :, :2] = np.linspace([100, 50], [300, 150], 17)
    points[0, :, 2] = 0.8
    points[1, :, 2] = 0.9
    net = network([[100, 50, 300, 150, 0.7], [0, 0, 10, 10, 0.01]], points)
    frame = np.zeros((640, 1280, 3), np.uint8)  # scaled down by half to fit 640 wide
    boxes, scores, xy, conf = net(frame, 0.1)
    assert net.session.seen.shape == (1, 3, 640, 640)
    assert boxes == pytest.approx(np.array([[200, 100, 600, 300]]))
    assert scores == pytest.approx([0.8])
    assert xy[0, -1] == pytest.approx([600, 300])
    assert conf.shape == (1, 17)
    assert net(frame, 0.8) is None
