"""Head turn estimate from COCO keypoints: nose, eyes, ears, shoulders.

The camera looks down at the baby lying on its back, so it sees the face head-on when the baby
looks at the ceiling. Turning the head sideways from this view is the same as turning your head
in front of an ordinary webcam, so the algorithm can be tested on yourself.

The head is modelled as a ball of radius R. Turned by angle a, the face center (nose and eyes)
moves sideways by ~0.9 * R * sin(a), while the ears sit at +-R * cos(a). Hence
score ~ 0.9 * tan(a): 0 is straight, +-0.35 is ~20 degrees, +-0.5 ~30, +-1 is a full profile.

Left and right are always the baby's own sides, not the frame's: score > 0 means facing the
left shoulder. "Sideways" is perpendicular to the shoulders -> head axis, so it does not matter
how the baby lies in the crib. A mirrored image (camera mirror/flip) flips the sign.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .i18n import t

NOSE, LEFT_EYE, RIGHT_EYE, LEFT_EAR, RIGHT_EAR, LEFT_SHOULDER, RIGHT_SHOULDER = range(7)

# Half the head width in units of the eye distance (~1.2 for adults and infants alike).
HALF_HEAD_TO_EYE_DIST = 1.2
# Gain for the "nose vs eyes" fallback: the nose shifts much less relative to the eyes than the
# face does relative to the ears. A rough guess; tune it on recordings from the real camera.
EYES_ONLY_GAIN = 3.0


class Side(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"
    UNKNOWN = "unknown"  # baby in frame, but the face can't be made out (turned away, covered)
    ABSENT = "absent"


def side_label(side: Side) -> str:
    return t(f"side.{side.value}")

_DIRECTIONS = {"up": (0.0, -1.0), "down": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}


@dataclass
class HeadEstimate:
    side: Side
    # > 0: facing left (toward the baby's left shoulder), < 0: right. None if it can't be estimated.
    score: float | None = None
    # A code like "both_ears": stored in the database as is, translated only for display.
    method: str = ""
    axis_source: str = ""  # where the "up" axis came from: "shoulders", "body" or "config"
    # Unit vector "up along the body" in frame coordinates, used to draw the preview.
    axis: tuple[float, float] | None = None

    @property
    def code(self) -> str:
        return f"{self.method}/{self.axis_source}" if self.axis_source else self.method

    def describe(self) -> str:
        text = t(f"method.{self.method}") if self.method else ""
        if not self.axis_source:
            return text
        return t("method.with_axis", method=text, axis=t(f"axis.{self.axis_source}"))


def classify(score: float, center_threshold: float) -> Side:
    if score > center_threshold:
        return Side.LEFT
    if score < -center_threshold:
        return Side.RIGHT
    return Side.CENTER


def _body_axis(
    xy: np.ndarray,
    visible: np.ndarray,
    head_center: np.ndarray,
    body_up: np.ndarray | None,
    fallback: str,
) -> tuple[np.ndarray, str]:
    if visible[LEFT_SHOULDER] and visible[RIGHT_SHOULDER]:
        up = head_center - (xy[LEFT_SHOULDER] + xy[RIGHT_SHOULDER]) / 2
        norm = float(np.linalg.norm(up))
        if norm > 1e-6:
            return up / norm, "shoulders"
    if body_up is not None:
        return np.asarray(body_up, dtype=float), "body"
    if fallback not in _DIRECTIONS:
        raise ValueError(t("err.fallback_direction"))
    return np.array(_DIRECTIONS[fallback]), "config"


def estimate_head(
    xy: np.ndarray,
    conf: np.ndarray,
    *,
    min_conf: float,
    center_threshold: float,
    mirrored: bool = False,
    fallback_head_direction: str = "up",
    body_up: np.ndarray | None = None,
) -> HeadEstimate:
    """body_up: unit vector shoulders -> head found earlier (on the first detector pass), used
    when the shoulders are not visible now. fallback_head_direction is the last resort."""
    xy = np.asarray(xy, dtype=float)
    visible = np.asarray(conf, dtype=float) >= min_conf
    both_eyes = bool(visible[LEFT_EYE] and visible[RIGHT_EYE])

    if not visible[NOSE] and not both_eyes:
        return HeadEstimate(Side.UNKNOWN, method="face_hidden")

    face_idx = [i for i in (NOSE, LEFT_EYE, RIGHT_EYE) if visible[i]]
    ear_idx = [i for i in (LEFT_EAR, RIGHT_EAR) if visible[i]]
    head_center = xy[face_idx + ear_idx].mean(axis=0)
    up, axis_source = _body_axis(xy, visible, head_center, body_up, fallback_head_direction)

    # Unit vector toward the baby's left shoulder. The baby faces the camera, so in an unmirrored
    # frame its left side is to the right of the "up" axis.
    toward_left = np.array([-up[1], up[0]])
    if mirrored:
        toward_left = -toward_left

    def lat(point: np.ndarray) -> float:
        return float(np.dot(point, toward_left))

    face = lat(xy[face_idx].mean(axis=0))
    eye_dist = float(np.linalg.norm(xy[LEFT_EYE] - xy[RIGHT_EYE])) if both_eyes else 0.0

    # Ignore the model's left/right ear labels: it mixes them up on a turned head, while the ear
    # position relative to the face is more reliable.
    if len(ear_idx) == 2:
        a, b = sorted(lat(xy[i]) for i in ear_idx)
        width = b - a
        if width > 1e-6:
            score = (2 * face - a - b) / width
        else:
            score = float(np.sign(face - a))
        method = "both_ears"
    elif len(ear_idx) == 1:
        offset = face - lat(xy[ear_idx[0]])
        if eye_dist > 1e-6:
            # Facing the camera, face to ear is ~half the head width. The further beyond that,
            # the stronger the turn (the other ear is then hidden in the mattress).
            excess = abs(offset) / eye_dist - HALF_HEAD_TO_EYE_DIST
            score = float(np.sign(offset)) * max(0.0, excess) / HALF_HEAD_TO_EYE_DIST
            method = "ear_and_eyes"
        else:
            score = float(np.sign(offset))
            method = "one_ear_profile"
    elif visible[NOSE] and eye_dist > 1e-6:
        eyes_mid = (lat(xy[LEFT_EYE]) + lat(xy[RIGHT_EYE])) / 2
        score = EYES_ONLY_GAIN * (lat(xy[NOSE]) - eyes_mid) / eye_dist
        method = "nose_and_eyes"
    else:
        return HeadEstimate(
            Side.UNKNOWN, method="ears_hidden", axis_source=axis_source, axis=(up[0], up[1])
        )

    score = float(np.clip(score, -1.0, 1.0))
    return HeadEstimate(
        classify(score, center_threshold),
        score,
        method=method,
        axis_source=axis_source,
        axis=(float(up[0]), float(up[1])),
    )
