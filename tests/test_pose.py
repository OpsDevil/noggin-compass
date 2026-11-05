import math

import numpy as np
import pytest

from noggin_compass.pose import MEMORY_SECONDS, Person, PoseDetector, UprightView, body_up, face_size


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
        self.min_face_size = 0.0
        self.faceless_seconds = 30.0
        self._memory = None
        self._face_seen = None
        self.first = None
        self.crops = []  # second-pass results, in call order

    def detect(self, frame, min_person_conf=None, min_person_area=None):
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
    assert det._memory == (5.0, pytest.approx([120, 110, 520, 310]), pytest.approx([1.0, 0.0]))


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


def with_face(score: float, span: float) -> Person:
    """A find whose nose and ears are `span` pixels apart. In a second pass these are crop
    pixels: the 64 px crop shows a 400 px box, so in the frame the face is 8 times larger."""
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[0], xy[3], xy[4] = (300, 200), (300 - span / 2, 200), (300 + span / 2, 200)
    conf[0], conf[3], conf[4] = 0.9, 0.9, 0.9
    return Person(np.zeros(4), score, xy, conf)


def test_face_size_counts_only_confident_face_points():
    person = with_face(0.5, 100.0)
    assert face_size(person, 0.4) == pytest.approx(100.0)
    person.conf[4] = 0.1
    assert face_size(person, 0.4) == pytest.approx(50.0)
    person.conf[3] = 0.1
    assert face_size(person, 0.4) == 0.0  # a lone point says nothing about the size
    # Shoulders are not the face.
    person.xy[5], person.conf[5] = (0, 0), 0.9
    assert face_size(person, 0.4) == 0.0


def test_find_with_a_tiny_face_is_not_the_baby_and_is_not_remembered():
    det = ScriptedDetector()
    det.min_face_size = 0.015  # ~10 px of a 640 px frame
    # A bird in the sheet print, found in an empty crib.
    det.first = lying_baby(0.2)
    det.crops = [with_face(0.3, 0.6)]
    assert det.detect_upright(FRAME, 0.4, now=0.0) is None
    assert det._memory is None

    det.first = lying_baby(0.5)
    det.crops = [with_face(0.6, 8.0)]
    assert det.detect_upright(FRAME, 0.4, now=1.0).score == 0.6
    assert det._memory is not None


def faceless(score: float) -> Person:
    """A find with shoulders but no face: a baby turned away, or a onesie laid out flat."""
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[5], xy[6] = (350, 250), (350, 150)
    conf[5], conf[6] = 0.9, 0.9
    return Person(np.zeros(4), score, xy, conf)


def test_find_without_a_face_counts_only_shortly_after_the_face():
    det = ScriptedDetector()
    det.min_face_size = 0.015
    # Clothes laid out in the crib: the shape of a person, but no face, and no baby seen before.
    det.first = lying_baby(0.6)
    det.crops = [faceless(0.4)]
    assert det.detect_upright(FRAME, 0.4, now=0.0) is None

    det.crops = [with_face(0.6, 8.0)]  # the baby, face up
    assert det.detect_upright(FRAME, 0.4, now=100.0) is not None
    # Asleep in profile: the face is hidden on some frames. Faceless finds do not extend the time.
    det.crops = [faceless(0.5), None]
    assert det.detect_upright(FRAME, 0.4, now=120.0) is not None
    det.crops = [faceless(0.5), None]
    assert det.detect_upright(FRAME, 0.4, now=131.0) is None


def test_first_pass_alone_counts_only_shortly_after_the_face():
    det = ScriptedDetector()
    det.min_face_size = 0.015
    det.first = lying_baby(0.4)
    det.first.xy[3], det.first.conf[3] = (460, 260), 0.9  # an ear, so the first pass has a face
    det.crops = [None]
    # The second pass sees nobody in the crop: without a face seen recently, nothing is found.
    assert det.detect_upright(FRAME, 0.4, now=0.0) is None

    det.crops = [with_face(0.6, 8.0)]
    det.detect_upright(FRAME, 0.4, now=100.0)
    det.crops = [None, None]  # the fresh crop and the remembered one
    person = det.detect_upright(FRAME, 0.4, now=110.0)
    assert person is not None and person.score == 0.4
    assert person.up is not None  # the first pass axis is kept for the head estimate


def test_without_the_check_everything_counts():
    det = ScriptedDetector()
    det.first = lying_baby(0.6)
    det.crops = [faceless(0.4)]
    assert det.detect_upright(FRAME, 0.4, now=0.0) is not None


def test_remembered_crop_without_the_baby_finds_nothing():
    det = ScriptedDetector()
    det.min_face_size = 0.015
    det.first = lying_baby(0.5)
    det.crops = [with_face(0.6, 8.0)]
    det.detect_upright(FRAME, 0.4, now=0.0)

    # The baby was taken out: the old crop now shows the sheet print.
    det.first = None
    det.crops = [with_face(0.2, 0.5)]
    assert det.detect_upright(FRAME, 0.4, now=5.0) is None


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


def detector_seeing(boxes, min_person_area, min_face_size=0.0):
    det = object.__new__(PoseDetector)
    det.model, det.device, det.imgsz = _Model(boxes), "cpu", 640
    det.min_person_conf, det.min_person_area = 0.1, min_person_area
    det.min_face_size, det.faceless_seconds, det._face_seen = min_face_size, 30.0, None
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


def test_single_pass_checks_the_face_too():
    baby = [100.0, 100.0, 500.0, 300.0]
    assert detector_seeing([baby], 0.03).detect_single(FRAME, 0.4) is not None
    # The fake model puts every keypoint at (0, 0): a face of zero size is not a baby's face.
    assert detector_seeing([baby], 0.03, min_face_size=0.015).detect_single(FRAME, 0.4) is None
