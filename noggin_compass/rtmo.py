"""RTMO pose models (OpenMMLab, Apache-2.0): one network finds the people in a picture together with
their 17 COCO keypoints. They run in ONNX form with ONNX Runtime."""

from __future__ import annotations

import logging
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import cv2
import numpy as np

from .i18n import t

log = logging.getLogger("noggin_compass")

URL = "https://download.openmmlab.com/mmpose/v1/projects/rtmo/onnx_sdk/"
# The same files on Hugging Face, for when the OpenMMLab server does not answer.
MIRROR = "https://huggingface.co/Tau-J/RTMPose/resolve/main/rtmo/onnx_sdk/"
ARCHIVES = {
    "rtmo-s": "rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.zip",
    "rtmo-m": "rtmo-m_16xb16-600e_body7-640x640-39e78cc4_20231211.zip",
    "rtmo-l": "rtmo-l_16xb16-600e_body7-640x640-b37118ce_20231211.zip",
}
INPUT_SIZE = 640  # the networks take a square picture of this size
PADDING = 114  # the grey the picture is padded with to a square, as in the networks' training
TIMEOUT = 30  # seconds without an answer before the mirror is tried
NMS_IOU = 0.45  # two people whose boxes overlap more than this are the same person


def is_rtmo(model: str) -> bool:
    return Path(model).name.lower().startswith("rtmo")


def weights(model: str, folder: Path) -> Path:
    """The ONNX file of `model`: a path to one, or a name such as rtmo-m, which is kept in `folder`.
    A known model missing on disk is downloaded first."""
    path = Path(model)
    if path.suffix != ".onnx":
        path = (folder if path.parent == Path(".") else path.parent) / f"{path.name}.onnx"
    if not path.exists():
        if path.stem not in ARCHIVES:
            raise ValueError(t("err.rtmo_model", model=model, names=", ".join(ARCHIVES)))
        log.info(t("pose.downloading", model=path.stem, path=path))
        download(ARCHIVES[path.stem], path)
    return path


def download(archive: str, to: Path) -> None:
    to.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / archive
        error = None
        for base in (URL, MIRROR):
            try:
                with urllib.request.urlopen(base + archive, timeout=TIMEOUT) as answer, open(local, "wb") as out:
                    shutil.copyfileobj(answer, out)
                break
            except OSError as failed:
                error = failed
        else:
            raise error
        with zipfile.ZipFile(local) as files:
            onnx = next(name for name in files.namelist() if name.endswith(".onnx"))
            partial = to.with_suffix(".part")
            with files.open(onnx) as src, open(partial, "wb") as out:
                shutil.copyfileobj(src, out)
        partial.replace(to)


def _nms(boxes: np.ndarray, scores: np.ndarray, min_score: float) -> list[int]:
    """Indices of the people kept: scored above `min_score`, surest first, each one dropped that
    overlaps a surer one by more than NMS_IOU."""
    candidates = np.flatnonzero(scores > min_score)
    if not len(candidates):
        return []
    x1, y1, x2, y2 = boxes[candidates].T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores[candidates].argsort()[::-1]
    keep = []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(int(candidates[i]))
        width = np.maximum(0.0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]) + 1)
        height = np.maximum(0.0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]) + 1)
        inter = width * height
        order = rest[inter / (areas[i] + areas[rest] - inter) <= NMS_IOU]
    return keep


class Rtmo:
    """An RTMO model on the CPU."""

    def __init__(self, path: Path):
        import onnxruntime

        self.session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def __call__(self, frame: np.ndarray, min_score: float):
        """Boxes (n, 4), scores (n,), keypoints (n, 17, 2) in pixels of `frame` and their
        confidences (n, 17) of the people found, or None."""
        height, width = frame.shape[:2]
        ratio = min(INPUT_SIZE / height, INPUT_SIZE / width)
        size = (int(width * ratio), int(height * ratio))
        picture = np.full((INPUT_SIZE, INPUT_SIZE, 3), PADDING, np.uint8)
        picture[: size[1], : size[0]] = cv2.resize(frame, size, interpolation=cv2.INTER_LINEAR)
        blob = np.ascontiguousarray(picture.transpose(2, 0, 1)[None], dtype=np.float32)
        people, points = self.session.run(None, {self.input: blob})
        keep = _nms(people[0, :, :4] / ratio, people[0, :, 4], min_score)
        if not keep:
            return None
        xy = points[0, keep, :, :2] / ratio
        conf = points[0, keep, :, 2]
        # A person's box is the one around its points and its score is their mean confidence:
        # the network was checked against the hand-labelled frames this way.
        boxes = np.concatenate([xy.min(axis=1), xy.max(axis=1)], axis=1)
        return boxes, conf.mean(axis=1), xy, conf
