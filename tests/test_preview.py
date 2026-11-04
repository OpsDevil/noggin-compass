import numpy as np

from noggin_compass.head import HeadEstimate, Side, estimate_head
from noggin_compass.pose import Person
from noggin_compass.preview import draw, fit

from .test_head import synthetic_keypoints


def test_draw_keeps_frame_size_and_handles_empty_frames():
    frame = np.full((480, 640, 3), 90, dtype=np.uint8)
    xy, conf = synthetic_keypoints(45)
    xy, conf = np.vstack([xy[:7], np.zeros((10, 2))]), np.concatenate([conf[:7], np.zeros(10)])
    person = Person(np.array([200.0, 120.0, 460.0, 420.0]), 0.9, xy, conf)
    estimate = estimate_head(xy, conf, min_conf=0.4, center_threshold=0.35)

    assert draw(frame, person, estimate, 0.4).shape == frame.shape
    assert draw(frame, None, HeadEstimate(Side.ABSENT), 0.4).shape == frame.shape


def test_fit_only_shrinks():
    small = np.zeros((100, 200, 3), dtype=np.uint8)
    assert fit(small) is small
    assert fit(np.zeros((1000, 2800, 3), dtype=np.uint8)).shape == (500, 1400, 3)
