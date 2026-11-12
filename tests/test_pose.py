import math

import numpy as np
import pytest

from noggin_compass.config import DetectorConfig
from noggin_compass.pose import Person, PoseDetector, UprightView, body_up, head_of


@pytest.mark.parametrize("angle_deg", [0, 37, 90, 180, 270, -120])
def test_upright_view_puts_head_on_top_and_maps_back(angle_deg):
    a = math.radians(angle_deg)
    up = np.array([math.cos(a), math.sin(a)])
    view = UprightView.around(np.array([100.0, 200.0, 500.0, 400.0]), up, 640, 1.3)

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
        self.cfg = DetectorConfig(imgsz=64)
        self._memory = None
        self.first = None
        self.crops = []  # second-pass results, in call order

    def detect(self, frame, min_person_conf=None, min_person_area=None, within=None):
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
    assert det.detect_upright(FRAME, 0.4, now=det.cfg.memory_seconds + 1) is None
    assert det.crops, "the stale crop must not be tried"


def test_memory_seconds_sets_how_long_the_old_crop_is_tried():
    det = ScriptedDetector()
    det.cfg.memory_seconds = 5
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    det.first = None
    det.crops = [Person(np.zeros(4), 0.3, np.ones((17, 2)), np.ones(17))]
    assert det.detect_upright(FRAME, 0.4, now=10.0) is None
    assert det.crops, "the crop is older than the limit and must not be tried"


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


def test_fresh_box_away_from_the_baby_loses_even_when_stronger():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.5, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    # The first pass misses the baby and finds the rolled bumper beside its head instead. The
    # bumper's crop catches the edge of the real head and scores higher than the baby's crop.
    det.first = lying_baby(0.3, box=(520.0, 100.0, 630.0, 300.0))
    det.crops = [
        Person(np.zeros(4), 0.7, np.ones((17, 2)), np.ones(17)),  # fresh crop: the bumper
        Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17)),  # remembered crop: the baby
    ]
    person = det.detect_upright(FRAME, 0.4, now=5.0)
    assert person.score == 0.4
    assert person.box == pytest.approx([100, 100, 500, 300])
    assert det._memory[1] == pytest.approx([100, 100, 500, 300])


def test_stronger_fresh_box_on_the_baby_wins_and_is_remembered():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    det.first = lying_baby(0.5, box=(120.0, 110.0, 520.0, 310.0))
    det.crops = [
        Person(np.zeros(4), 0.6, np.ones((17, 2)), np.ones(17)),
        Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17)),
    ]
    person = det.detect_upright(FRAME, 0.4, now=5.0)
    assert person.score == 0.6
    assert det._memory[:3] == (5.0, pytest.approx([120, 110, 520, 310]), pytest.approx([1.0, 0.0]))


def test_baby_beats_a_remembered_bumper_even_when_weaker():
    det = ScriptedDetector()
    det.first = lying_baby(0.3, box=(520.0, 100.0, 630.0, 300.0))
    det.crops = [Person(np.zeros(4), 0.7, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)  # only the bumper was found, so it is remembered

    det.first = lying_baby(0.5)
    det.crops = [
        Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17)),  # fresh crop: the baby
        Person(np.zeros(4), 0.7, np.ones((17, 2)), np.ones(17)),  # remembered crop: the bumper
    ]
    person = det.detect_upright(FRAME, 0.4, now=5.0)
    assert person.box == pytest.approx([100, 100, 500, 300])
    assert det._memory[1] == pytest.approx([100, 100, 500, 300])


def test_baby_moved_elsewhere_is_found_once_the_old_place_is_empty():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    det.crops = [Person(np.zeros(4), 0.4, np.ones((17, 2)), np.ones(17))]
    det.detect_upright(FRAME, 0.4, now=0.0)

    det.first = lying_baby(0.5, box=(520.0, 100.0, 630.0, 300.0))
    det.crops = [Person(np.zeros(4), 0.5, np.ones((17, 2)), np.ones(17)), None]
    person = det.detect_upright(FRAME, 0.4, now=5.0)
    assert person.box == pytest.approx([520, 100, 630, 300])


class _Tensor:
    """Just enough of a torch tensor for PoseDetector.detect."""

    def __init__(self, values):
        self.values = np.asarray(values, dtype=float)

    def cpu(self):
        return self

    def numpy(self):
        return self.values

    def __getitem__(self, i):
        return _Tensor(self.values[i])

    def __len__(self):
        return len(self.values)

    def __float__(self):
        return float(self.values)


class _Model:
    def __init__(self, boxes):
        n = len(boxes)
        boxes_obj = type("Boxes", (), {"xyxy": _Tensor(boxes), "conf": _Tensor([0.5] * n), "__len__": lambda self: n})
        keypoints = type("Keypoints", (), {"xy": _Tensor(np.zeros((n, 17, 2))), "conf": _Tensor(np.ones((n, 17)))})
        self.result = type("Result", (), {"boxes": boxes_obj(), "keypoints": keypoints()})()

    def predict(self, frame, **kwargs):
        return [self.result]


def detector_seeing(boxes, min_person_area):
    det = object.__new__(PoseDetector)
    det.model = _Model(boxes)
    det.cfg = DetectorConfig(min_person_conf=0.1, min_person_area=min_person_area)
    return det


def test_people_smaller_than_min_area_are_ignored():
    # A toy on the crib rail: 2% of a 640x480 frame.
    toy = [10.0, 10.0, 88.0, 89.0]
    assert detector_seeing([toy], 0.03).detect(FRAME) is None
    assert detector_seeing([toy], 0.0).detect(FRAME) is not None
    # The largest one is still picked when it is big enough.
    baby = [100.0, 100.0, 500.0, 300.0]
    person = detector_seeing([toy, baby], 0.03).detect(FRAME)
    assert person.box == pytest.approx(baby)
    # The second pass looks at a crop around the baby and ignores the limit.
    assert detector_seeing([toy], 0.03).detect(FRAME, min_person_area=0.0) is not None


def test_on_the_crop_the_person_inside_the_babys_box_is_taken_not_the_largest():
    baby, toy = [200.0, 100.0, 400.0, 300.0], [0.0, 320.0, 640.0, 480.0]  # the toy is larger, below
    detector = detector_seeing([baby, toy], 0.0)
    assert detector.detect(FRAME).box == pytest.approx(toy)  # without a box to look in: the largest
    assert detector.detect(FRAME, within=np.array([150.0, 50.0, 450.0, 350.0])).box == pytest.approx(baby)
    assert detector.detect(FRAME, within=np.array([0.0, 0.0, 100.0, 100.0])) is None  # nobody there


def test_points_far_outside_the_babys_box_are_dropped():
    detector = ScriptedDetector()
    box = np.array([100.0, 100.0, 500.0, 300.0])
    up = np.array([1.0, 0.0])
    view = UprightView.around(box, up, detector.cfg.imgsz, detector.cfg.upright_margin)
    matrix = view.matrix()

    def to_crop(points):
        return np.asarray(points, dtype=float) @ matrix[:, :2].T + matrix[:, 2]

    xy, conf = np.zeros((17, 2)), np.full(17, 0.9)
    xy[0] = to_crop([[450.0, 200.0]])[0]  # the nose, on the baby
    xy[1] = to_crop([[300.0, 395.0]])[0]  # an "eye" well below the box: a toy next to the baby
    detector.crops = [Person(np.zeros(4), 0.8, xy, conf)]
    person = detector._second_pass(np.zeros((400, 600, 3), np.uint8), box, up)
    assert person.conf[0] == pytest.approx(0.9) and person.xy[0] == pytest.approx([450.0, 200.0])
    assert person.conf[1] == 0.0 and not person.xy[1].any()


def test_a_face_far_from_the_head_is_not_the_babys():
    # The box also holds a toy with a face on the rail; the crop finds no baby and takes the toy.
    detector = ScriptedDetector()
    baby = lying_baby(0.9)  # head at (450, 200), shoulders at x = 350: the neck is 100 long
    head = head_of(baby, 0.4)
    box, up = baby.box, head.up
    matrix = UprightView.around(box, up, detector.cfg.imgsz, detector.cfg.upright_margin).matrix()

    def crop_with_face_at(x, y):
        xy, conf = np.zeros((17, 2)), np.zeros(17)
        xy[:3] = np.array([[x, y], [x - 5, y - 5], [x - 5, y + 5]]) @ matrix[:, :2].T + matrix[:, 2]
        conf[:3] = 0.9
        return Person(np.zeros(4), 0.8, xy, conf)

    frame = np.zeros((400, 600, 3), np.uint8)
    detector.crops = [crop_with_face_at(200.0, 290.0)]  # 270 away: more than detector.face_reach necks
    assert detector._second_pass(frame, box, up, head, 0.4) is None
    detector.crops = [crop_with_face_at(440.0, 230.0)]  # the baby's own face, by the head
    assert detector._second_pass(frame, box, up, head, 0.4) is not None
    detector.crops = [crop_with_face_at(200.0, 290.0)]  # without a head to compare with: kept
    assert detector._second_pass(frame, box, up) is not None


def test_when_the_crop_finds_a_face_elsewhere_the_side_is_not_guessed():
    det = ScriptedDetector()
    det.first = lying_baby(0.5)
    xy, conf = np.zeros((17, 2)), np.zeros(17)
    view = UprightView.around(det.first.box, np.array([1.0, 0.0]), det.cfg.imgsz, det.cfg.upright_margin)
    matrix = view.matrix()
    xy[:3] = np.array([[150.0, 290.0]] * 3) @ matrix[:, :2].T + matrix[:, 2]
    conf[:3] = 0.9
    det.crops = [Person(np.zeros(4), 0.8, xy, conf)]
    person = det.detect_upright(FRAME, 0.4, now=0.0)
    assert not person.checked
    assert det._memory is None
