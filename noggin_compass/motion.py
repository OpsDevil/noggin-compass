"""How much the baby moves from one frame to the next: the basis for telling sleep from being awake.
A sleeping baby barely moves, an awake one turns, kicks and waves its arms."""

from __future__ import annotations

import cv2
import numpy as np

# Width the frame is shrunk to. Movement of arms and legs is still visible, while video noise and
# compression artifacts mostly average out.
WIDTH = 320
# Brightness change (0-255, after blur) that counts as movement of a pixel.
PIXEL_THRESHOLD = 12
# The box is widened so that a hand or a foot sticking out of it still counts.
BOX_MARGIN = 0.15
# After a reconnect the previous frame is too old to compare with.
MAX_GAP_SECONDS = 5.0


class MotionMeter:
    def __init__(self):
        self._prev: tuple[float, np.ndarray] | None = None

    def update(self, frame: np.ndarray, box: np.ndarray | None, now: float) -> float | None:
        """Share of pixels in the baby's box (0..1) that changed since the previous frame, or
        None if there is nothing to compare: no baby found or no recent previous frame."""
        h, w = frame.shape[:2]
        scale = WIDTH / w
        small = cv2.resize(frame, (WIDTH, max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.int16)
        prev, self._prev = self._prev, (now, gray)
        if box is None or prev is None or now - prev[0] > MAX_GAP_SECONDS or prev[1].shape != gray.shape:
            return None
        x1, y1, x2, y2 = (float(v) for v in box)
        dx, dy = BOX_MARGIN * (x2 - x1), BOX_MARGIN * (y2 - y1)
        gh, gw = gray.shape
        c1, r1 = max(0, int((x1 - dx) * scale)), max(0, int((y1 - dy) * scale))
        c2, r2 = min(gw, int((x2 + dx) * scale) + 1), min(gh, int((y2 + dy) * scale) + 1)
        if c2 <= c1 or r2 <= r1:
            return None
        diff = gray[r1:r2, c1:c2] - prev[1][r1:r2, c1:c2]
        # A colour-changing night light or the auto exposure shifts the whole picture at once.
        diff -= int(np.median(diff))
        return float(np.mean(np.abs(diff) > PIXEL_THRESHOLD))
