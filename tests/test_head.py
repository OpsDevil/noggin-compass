import math

import numpy as np
import pytest

from noggin_compass.head import Side, estimate_head

R = 50.0
EYE_ANGLE = math.radians(25)
VISIBLE, HIDDEN = 0.9, 0.05


def synthetic_keypoints(yaw_deg: float, rotate_deg: float = 0.0, mirror: bool = False, shoulders: bool = True):
    """Keypoints of a baby facing the camera with the top of the head toward the top of the frame
    and the head turned by yaw_deg (> 0 toward its own left shoulder). The whole baby is then
    rotated in the frame by rotate_deg."""
    yaw = math.radians(yaw_deg)
    xy = np.zeros((17, 2))
    conf = np.zeros(17)

    def put(i, angle, height, min_depth):
        # A point on the ball at `angle` from the direction toward the camera; the baby's left
        # shoulder is +x in the frame.
        lateral, depth = R * math.sin(angle), R * math.cos(angle)
        xy[i] = (lateral, height)
        conf[i] = VISIBLE if depth > min_depth * R else HIDDEN

    put(0, yaw, 0.0, -0.1)  # nose
    put(1, yaw + EYE_ANGLE, -0.3 * R, 0.2)  # left eye
    put(2, yaw - EYE_ANGLE, -0.3 * R, 0.2)  # right eye
    put(3, yaw + math.pi / 2, -0.1 * R, -0.3)  # left ear
    put(4, yaw - math.pi / 2, -0.1 * R, -0.3)  # right ear
    xy[5], xy[6] = (1.5 * R, 2.2 * R), (-1.5 * R, 2.2 * R)
    conf[5] = conf[6] = VISIBLE if shoulders else HIDDEN

    a = math.radians(rotate_deg)
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    xy = xy @ rot.T + (320, 240)
    if mirror:
        xy[:, 0] = 640 - xy[:, 0]
    return xy, conf


def estimate(xy, conf, **kw):
    return estimate_head(xy, conf, min_conf=0.4, center_threshold=0.35, **kw)


@pytest.mark.parametrize(
    "yaw, expected",
    [
        (0, Side.CENTER),
        (10, Side.CENTER),
        (-10, Side.CENTER),
        (30, Side.LEFT),
        (-30, Side.RIGHT),
        (60, Side.LEFT),
        (-60, Side.RIGHT),
        (90, Side.LEFT),
        (-90, Side.RIGHT),
    ],
)
@pytest.mark.parametrize("rotate", [0, 37, 90, 180, 270])
def test_side_does_not_depend_on_how_baby_lies_in_frame(yaw, expected, rotate):
    assert estimate(*synthetic_keypoints(yaw, rotate)).side == expected


def test_score_grows_with_yaw():
    scores = [estimate(*synthetic_keypoints(yaw)).score for yaw in (0, 10, 30, 60, 90)]
    assert scores == sorted(scores)
    assert abs(scores[0]) < 0.05
    assert scores[-1] == pytest.approx(1.0)


def test_mirrored_image_flips_sides_unless_configured():
    xy, conf = synthetic_keypoints(45, mirror=True)
    assert estimate(xy, conf).side == Side.RIGHT
    assert estimate(xy, conf, mirrored=True).side == Side.LEFT


@pytest.mark.parametrize("rotate, direction", [(0, "up"), (90, "right"), (180, "down"), (270, "left")])
def test_fallback_axis_when_shoulders_hidden(rotate, direction):
    xy, conf = synthetic_keypoints(45, rotate, shoulders=False)
    result = estimate(xy, conf, fallback_head_direction=direction)
    assert result.side == Side.LEFT
    assert result.axis_source == "config"


def test_one_ear_frontal_is_center():
    xy, conf = synthetic_keypoints(0)
    conf[3] = HIDDEN  # left ear missed although the head is straight
    assert estimate(xy, conf).side == Side.CENTER


def test_face_not_visible_is_unknown():
    xy, conf = synthetic_keypoints(0)
    conf[:3] = HIDDEN
    assert estimate(xy, conf).side == Side.UNKNOWN


def test_nose_and_eyes_only_gives_weak_estimate():
    xy, conf = synthetic_keypoints(0)
    conf[3:5] = HIDDEN
    result = estimate(xy, conf)
    assert result.side == Side.CENTER
    assert result.method == "nose_and_eyes"
