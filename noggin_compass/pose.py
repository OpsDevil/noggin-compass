"""Person and keypoint detection (YOLO pose, 17 COCO keypoints). Everything runs locally."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

LEFT_SHOULDER, RIGHT_SHOULDER = 5, 6
# Size of the crop around the baby relative to its bounding box.
UPRIGHT_MARGIN = 1.3
UPRIGHT_PERSON_CONF = 0.1
# How long the crop of the last found baby is tried again. Seen from above in a sleep sack, the
# baby is missed by the first pass on many frames, but a sleeping baby barely moves, so the old
# crop still finds it. The limit stops a stale crop from outliving a real change of position.
MEMORY_SECONDS = 60
MODELS_DIR = Path("models")


@dataclass
class Person:
    box: np.ndarray  # x1, y1, x2, y2
    score: float
    xy: np.ndarray  # (17, 2) in pixels
    conf: np.ndarray  # (17,)
    # Unit vector shoulders -> head from the first pass, used for the head estimate when the
    # second pass does not see the shoulders.
    up: np.ndarray | None = None


@dataclass
class UprightView:
    """Rotation and scale that put the baby head-up in a square crop."""

    rotation: np.ndarray  # 2x2
    scale: float
    center: np.ndarray  # crop center in the source frame
    size: int

    @classmethod
    def around(cls, box: np.ndarray, up: np.ndarray, size: int) -> UprightView:
        # Rotate so that the shoulders -> head direction becomes (0, -1), i.e. up in the image.
        angle = -math.pi / 2 - math.atan2(up[1], up[0])
        cos, sin = math.cos(angle), math.sin(angle)
        x1, y1, x2, y2 = box
        scale = size / (UPRIGHT_MARGIN * max(x2 - x1, y2 - y1))
        return cls(np.array([[cos, -sin], [sin, cos]]), scale, np.array([(x1 + x2) / 2, (y1 + y2) / 2]), size)

    def matrix(self) -> np.ndarray:
        """Matrix for cv2.warpAffine: source frame -> crop."""
        offset = self.size / 2 - self.scale * self.rotation @ self.center
        return np.hstack([self.scale * self.rotation, offset[:, None]])

    def to_frame(self, xy: np.ndarray) -> np.ndarray:
        """Map points from the crop back to source frame coordinates."""
        return (self.rotation.T @ ((xy - self.size / 2).T / self.scale)).T + self.center


def body_up(person: Person, min_conf: float) -> np.ndarray | None:
    """Unit vector shoulders -> head, or None if the shoulders or the head are not visible."""
    if person.conf[LEFT_SHOULDER] < min_conf or person.conf[RIGHT_SHOULDER] < min_conf:
        return None
    # A rough head center is enough for the axis, so low-confidence face points count too.
    head = [i for i in range(5) if person.conf[i] >= min_conf / 2 and person.xy[i].any()]
    if not head:
        return None
    up = person.xy[head].mean(axis=0) - (person.xy[LEFT_SHOULDER] + person.xy[RIGHT_SHOULDER]) / 2
    norm = float(np.linalg.norm(up))
    return up / norm if norm > 1e-6 else None


def _disable_telemetry() -> None:
    # Ultralytics sends anonymous usage stats by default. No video leaves the machine either way,
    # but there is no reason to send the stats.
    try:
        from ultralytics.utils.events import events

        events.enabled = False
    except Exception:
        pass


class PoseDetector:
    def __init__(
        self, model: str, device: str = "cpu", min_person_conf: float = 0.35, imgsz: int = 640
    ):
        if not Path(model).exists() and Path(model).parent == Path("."):
            # A bare name like yolo26m-pose.pt: ultralytics will download it, so keep it in models/.
            model = str(MODELS_DIR / model)
        if Path(model).exists():
            # Weights are already here, so block all ultralytics network requests (update checks,
            # stats). The variable is read at import time, so set it before the import.
            os.environ.setdefault("YOLO_OFFLINE", "1")
        from ultralytics import YOLO

        _disable_telemetry()
        Path(model).parent.mkdir(parents=True, exist_ok=True)
        # If the file is missing, ultralytics downloads the weights from GitHub once.
        self.model = YOLO(model)
        self.device = device
        self.min_person_conf = min_person_conf
        self.imgsz = imgsz
        self._memory: tuple[float, np.ndarray, np.ndarray] | None = None  # time, box, up

    def detect(self, frame: np.ndarray, min_person_conf: float | None = None) -> Person | None:
        conf = self.min_person_conf if min_person_conf is None else min_person_conf
        result = self.model.predict(
            frame, conf=conf, device=self.device, imgsz=self.imgsz, verbose=False
        )[0]
        if result.keypoints is None or result.boxes is None or len(result.boxes) == 0:
            return None
        boxes = result.boxes.xyxy.cpu().numpy()
        # In a crib, the person we want is the largest one in the frame.
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        i = int(areas.argmax())
        keypoints = result.keypoints
        xy = keypoints.xy[i].cpu().numpy()
        conf = keypoints.conf[i].cpu().numpy() if keypoints.conf is not None else np.ones(len(xy))
        return Person(boxes[i], float(result.boxes.conf[i]), xy, conf)

    def detect_upright(self, frame: np.ndarray, min_conf: float, now: float | None = None) -> Person | None:
        """Two passes. The model was trained on standing and sitting people and confuses face
        points, especially the ears, on a baby lying across the frame. The first pass finds the
        baby and its shoulders. The second runs on a crop rotated head-up, where the face is
        detected much better. Keypoints are returned in source frame coordinates.

        With `now` (seconds), the crop of the last baby found in the past MEMORY_SECONDS is tried
        too, and the second pass the model is more sure of wins."""
        first = self.detect(frame)
        up = body_up(first, min_conf) if first is not None else None
        fresh = self._second_pass(frame, first.box, up) if up is not None else None
        best = fresh
        if self._memory is not None and now is not None and now - self._memory[0] <= MEMORY_SECONDS:
            # Also wins over a fresh detection that is something else (a glowing toy, a hand).
            old = self._second_pass(frame, *self._memory[1:])
            if old is not None and (best is None or old.score > best.score):
                best = old
        if fresh is not None and best is fresh and now is not None:
            self._memory = (now, first.box, up)
        if best is None and first is not None:
            return replace(first, up=up)
        return best

    def _second_pass(self, frame: np.ndarray, box: np.ndarray, up: np.ndarray) -> Person | None:
        view = UprightView.around(box, up, self.imgsz)
        crop = cv2.warpAffine(frame, view.matrix(), (view.size, view.size))
        # The first pass already found the baby, so a lower person threshold is safe here: under
        # a blanket, person confidence on the crop can be low. Keypoint confidences from the first
        # pass are not trusted: it is confidently wrong on a baby lying across the frame.
        second = self.detect(crop, min_person_conf=UPRIGHT_PERSON_CONF)
        if second is None:
            return None
        xy = view.to_frame(second.xy)
        xy[~second.xy.any(axis=1)] = 0  # keypoints the model did not place stay at (0, 0)
        return Person(box, second.score, xy, second.conf, up)
