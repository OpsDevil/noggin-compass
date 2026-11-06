"""Debug image: which keypoints the model found, how confident it is, and what the program made
of them. The head is small in the frame, so it is also shown enlarged in the corner."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .head import HeadEstimate, Side, side_label
from .i18n import t
from .pose import Person

FACE = range(5)
_FONTS = ["/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"]

_SIDE_COLORS = {
    Side.LEFT: (60, 150, 255),
    Side.RIGHT: (255, 165, 0),
    Side.CENTER: (40, 200, 70),
    Side.UNKNOWN: (170, 170, 170),
    Side.ABSENT: (120, 120, 120),
}
FOUND = (255, 230, 0)  # confident keypoint
DOUBTFUL = (170, 170, 170)  # low confidence, not used in the estimate
UP_COLOR = (255, 255, 255)
LEFT_COLOR = (255, 60, 220)
INSET_SIZE = 420


@cache
def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, size: int, color, anchor="la") -> None:
    # Dark background, otherwise the text gets lost on a patterned sheet.
    font = _font(size)
    box = draw.textbbox(xy, text, font=font, anchor=anchor)
    draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
    draw.text(xy, text, font=font, fill=color, anchor=anchor)


def _dot(draw: ImageDraw.ImageDraw, x: float, y: float, r: float, found: bool) -> None:
    if found:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=FOUND, outline=(0, 0, 0), width=2)
    else:
        draw.ellipse((x - r, y - r, x + r, y + r), outline=DOUBTFUL, width=2)


def _arrow(draw, start, vector, length, color, width, text, size) -> None:
    end = (start[0] + vector[0] * length, start[1] + vector[1] * length)
    draw.line((start, end), fill=color, width=width)
    draw.ellipse((end[0] - width * 2, end[1] - width * 2, end[0] + width * 2, end[1] + width * 2), fill=color)
    _label(draw, (end[0] + vector[0] * 12, end[1] + vector[1] * 12), text, size, color, anchor="mm")


def _has_xy(person: Person, i: int) -> bool:
    return bool(person.xy[i].any())  # (0, 0) means the model gave no coordinates at all


def _head_inset(clean: Image.Image, person: Person, min_conf: float) -> Image.Image | None:
    """Enlarged head. Points are numbered and listed with their confidence below the image:
    labels next to the points would overlap, the face is too small."""
    points = [i for i in FACE if _has_xy(person, i)]
    if not points:
        return None
    xy = person.xy[points]
    center = xy.mean(axis=0)
    spread = float(np.ptp(xy, axis=0).max()) if len(points) > 1 else 0.0
    half = max(spread * 1.6, 50.0)
    left, top = center - half
    crop = clean.crop((int(left), int(top), int(left + 2 * half), int(top + 2 * half)))
    zoom = INSET_SIZE / (2 * half)

    line = 30
    inset = Image.new("RGB", (INSET_SIZE, INSET_SIZE + line * len(FACE) + 12), (0, 0, 0))
    inset.paste(crop.resize((INSET_SIZE, INSET_SIZE), Image.LANCZOS), (0, 0))
    draw = ImageDraw.Draw(inset)
    for i in points:
        x, y = (person.xy[i] - (left, top)) * zoom
        found = person.conf[i] >= min_conf
        _dot(draw, x, y, 8, found)
        _label(draw, (x + 10, y - 10), str(i + 1), 18, FOUND if found else DOUBTFUL, "lb")
    for row, i in enumerate(FACE):
        conf = f"{person.conf[i]:.0%}" if _has_xy(person, i) else t("preview.no_point")
        found = _has_xy(person, i) and person.conf[i] >= min_conf
        mark = "●" if found else "○"
        draw.text((12, INSET_SIZE + 8 + row * line), f"{mark} {i + 1}  {t(f'kp.{i}')}  {conf}",
                  font=_font(20), fill=FOUND if found else DOUBTFUL)
    draw.rectangle((0, 0, inset.width - 1, inset.height - 1), outline=(255, 255, 255), width=3)
    return inset


def draw(
    frame: np.ndarray,
    person: Person | None,
    estimate: HeadEstimate,
    min_conf: float,
    mirrored: bool = False,
    notes: Sequence[str] = (),
) -> np.ndarray:
    """The frame with the points and the estimate. `notes` are extra lines in the bottom left
    corner, such as the thermometer readings."""
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    clean = image.copy()
    draw_ = ImageDraw.Draw(image)
    unit = max(1.0, frame.shape[1] / 900)  # scale lines and text to the frame size
    color = _SIDE_COLORS[estimate.side]

    if person is not None:
        x1, y1, x2, y2 = person.box.tolist()
        draw_.rectangle((x1, y1, x2, y2), outline=color, width=round(2 * unit))
        for i in range(7):
            if _has_xy(person, i):
                x, y = person.xy[i]
                _dot(draw_, x, y, 4 * unit, person.conf[i] >= min_conf)
        for i in (5, 6):
            if _has_xy(person, i):
                x, y = person.xy[i]
                _label(draw_, (x + 8 * unit, y), f"{t(f'kp.{i}')} {person.conf[i]:.0%}",
                       round(13 * unit), FOUND if person.conf[i] >= min_conf else DOUBTFUL, "lm")

        face = [i for i in FACE if person.conf[i] >= min_conf]
        if estimate.axis is not None and face:
            start = tuple(person.xy[face].mean(axis=0))
            up = estimate.axis
            toward_left = (-up[1], up[0]) if not mirrored else (up[1], -up[0])
            length = 0.22 * max(x2 - x1, y2 - y1)
            width = round(3 * unit)
            _arrow(draw_, start, up, length, UP_COLOR, width, t("preview.crown"), round(13 * unit))
            _arrow(draw_, start, toward_left, length, LEFT_COLOR, width, t("preview.baby_left"), round(13 * unit))

        inset = _head_inset(clean, person, min_conf)
        if inset is not None:
            image.paste(inset, (image.width - INSET_SIZE - 10, 10))

    score = f"   score {estimate.score:+.2f}" if estimate.score is not None else ""
    size = round(20 * unit)
    _label(draw_, (12, 12), f"{side_label(estimate.side).upper()}{score}", size, color)
    _label(draw_, (12, 12 + size * 1.6), estimate.describe(), round(14 * unit), (230, 230, 230))
    note_size = round(16 * unit)
    for row, note in enumerate(reversed(notes)):
        _label(draw_, (12, image.height - 12 - row * note_size * 1.6), note, note_size, (230, 230, 230), "lb")
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def fit(image: np.ndarray, max_width: int = 1400) -> np.ndarray:
    if image.shape[1] <= max_width:
        return image
    scale = max_width / image.shape[1]
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
