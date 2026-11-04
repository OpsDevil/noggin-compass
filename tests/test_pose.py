import math

import numpy as np
import pytest

from noggin_compass.pose import MEMORY_SECONDS, Person, PoseDetector, UprightView, body_up


@pytest.mark.parametrize("angle_deg", [0, 37, 90, 180, 270, -120])
def test_upright_view_puts_head_on_top_and_maps_back(angle_deg):
    a = math.radians(angle_deg)
    up = np.array([math.cos(a), math.sin(a)])
    view = UprightView.around(np.array([100.0, 200.0, 500.0, 400.0]), up, 640)

    rotated = view.rotation @ up
    assert rotated == pytest.approx([0.0, -1.0], abs=1e-9)

    points = np.array([[120.0, 210.0], [300.0, 300.0], [480.0, 390.0]])
    matrix = view.matrix()
    in_crop = points @ matrix[:, :2].T + matrix[:, 2]
    assert view.to_frame(in_crop) == pytest.approx(points)
    # The box center maps to the crop center.
    assert in_crop[1] == pytest.approx([320.0, 320.0])


def test_body_up_needs_shoulders_and_head():
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[0], xy[5], xy[6] = (300, 100), (250, 200), (350, 200)
    conf[0], conf[5], conf[6] = 0.9, 0.9, 0.9
    person = Person(np.zeros(4), 0.9, xy, conf)
    assert body_up(person, 0.4) == pytest.approx([0.0, -1.0])

    conf[5] = 0.1
    assert body_up(person, 0.4) is None


def lying_baby(score: float, box=(100.0, 100.0, 500.0, 300.0)) -> Person:
    """A baby whose shoulders and nose the model sees, head toward +x."""
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[0], xy[5], xy[6] = (450, 200), (350, 250), (350, 150)
    conf[0], conf[5], conf[6] = 0.9, 0.9, 0.9
    return Person(np.array(box), score, xy, conf)


class ScriptedDetector(PoseDetector):
    """PoseDetector without a model: the first pass and each crop return what the test says."""

    def __init__(self):
        self.imgsz = 64
        self._memory = None
        self.first = None
        self.crops = []  # second-pass results, in call order

    def detect(self, frame, min_person_conf=None):
        if min_person_conf is None:
            return self.first
        return self.crops.pop(0)


FRAME = np.zeros((480, 640, 3), np.uint8)


def test_second_pass_carries_first_pass_axis():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    person = det.detect_upright(FRAME, 0.4, now=0.0)
    assert person.score == 0.4
    assert person.up == pytest.approx([1.0, 0.0])


def test_remembered_crop_used_when_first_pass_misses():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    det.first = None
    det.crops = [Person(np.zeros(4), 0.3, np.ones((17, 2)), np.ones(17))]
    person = det.detect_upright(FRAME, 0.4, now=10.0)
    assert person is not None and person.score == 0.3
    assert person.box == pytest.approx([100, 100, 500, 300])
    assert person.up == pytest.approx([1.0, 0.0])

    det.crops = [Person(np.zeros(4), 0.3, np.ones((17, 2)), np.ones(17))]
    assert det.detect_upright(FRAME, 0.4, now=MEMORY_SECONDS + 1) is None
    assert det.crops, "the stale crop must not be tried"


def test_remembered_crop_beats_a_weaker_fresh_detection():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.6, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    # Something else at the edge of the crib (a glowing toy) is found instead of the baby.
    det.first = lying_baby(0.2, box=(0.0, 0.0, 80.0, 300.0))
    det.crops = [
        Person(np.zeros(4), 0.15, np.ones((17, 2)), np.ones(17)),  # fresh crop: the toy
        Person(np.zeros(4), 0.5, np.ones((17, 2)), np.ones(17)),  # remembered crop: the baby
    ]
    person = det.detect_upright(FRAME, 0.4, now=5.0)
    assert person.score == 0.5
    assert person.box == pytest.approx([100, 100, 500, 300])
    # The toy did not replace the remembered crop.
    assert det._memory[1] == pytest.approx([100, 100, 500, 300])


def test_without_time_nothing_is_remembered():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4)
    det.first = None
    assert det.detect_upright(FRAME, 0.4) is None
