import math

import numpy as np
import pytest

from noggin_compass.pose import Person, UprightView, body_up


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
