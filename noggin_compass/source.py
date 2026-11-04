"""Frame sources: an RTSP camera, a local Mac camera or a video file."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

from . import keychain
from .i18n import t

# RTSP over TCP is noticeably more stable on Wi-Fi than the default UDP.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
# Tapo sends a non-standard SEI in every frame, and ffmpeg logs it as an error. Keep only fatal
# messages: dropped connections show up anyway as reconnects.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "8")

log = logging.getLogger(__name__)

Frame = np.ndarray

# The camera URL has {password} in place of the password. Lookup order: the environment variable
# (handy to keep in .env next to the config), camera.password in the config, the macOS Keychain.
PASSWORD_ENV = "NOGGIN_CAMERA_PASSWORD"
KEYCHAIN_SERVICE = "noggin-compass-camera"


def resolve_password(source: str | int, config_password: str | None = None) -> str | int:
    if not (isinstance(source, str) and "{password}" in source):
        return source
    password = os.environ.get(PASSWORD_ENV) or config_password or keychain.read(KEYCHAIN_SERVICE)
    if not password:
        raise RuntimeError(t("err.password", env=PASSWORD_ENV, service=KEYCHAIN_SERVICE))
    return source.replace("{password}", quote(password, safe=""))


def _parse_source(source: str | int) -> str | int:
    if isinstance(source, str) and source.isdigit():
        return int(source)
    return source


def is_file_source(source: str | int) -> bool:
    source = _parse_source(source)
    return isinstance(source, str) and "://" not in source and Path(source).exists()


class LiveSource:
    """Live stream. A background thread keeps reading frames and only the latest one is handed
    out; otherwise, at 1 analysed frame per second, the RTSP buffer builds up delay."""

    STALE_AFTER = 5.0
    RECONNECT_DELAY = 5.0

    def __init__(self, source: str | int, label: str | int | None = None):
        self.source = _parse_source(source)
        # For logs: the URL without the password.
        self.label = label if label is not None else source
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._frame_time = 0.0
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.source)
        if isinstance(self.source, str):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _reader(self) -> None:
        cap = self._open()
        while self._running:
            ok, frame = cap.read() if cap.isOpened() else (False, None)
            if ok:
                with self._lock:
                    self._frame, self._frame_time = frame, time.time()
                continue
            log.warning(t("warn.no_frames", source=self.label, delay=self.RECONNECT_DELAY))
            cap.release()
            time.sleep(self.RECONNECT_DELAY)
            cap = self._open()
        cap.release()

    def frames(self, sample_fps: float) -> Iterator[tuple[datetime, Frame]]:
        period = 1.0 / sample_fps
        next_tick = time.time()
        while self._running:
            next_tick += period
            delay = next_tick - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.time()
            with self._lock:
                frame, frame_time = self._frame, self._frame_time
            if frame is None or time.time() - frame_time > self.STALE_AFTER:
                continue
            yield datetime.fromtimestamp(frame_time), frame

    def close(self) -> None:
        self._running = False


class FileSource:
    """Video file, e.g. a recording of a night. Frame time = start + position in the video."""

    def __init__(self, path: str, start: datetime | None = None):
        self.path = path
        self.start = start or datetime.fromtimestamp(Path(path).stat().st_mtime)
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise RuntimeError(t("err.video", path=path))

    def frames(self, sample_fps: float) -> Iterator[tuple[datetime, Frame]]:
        fps = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
        step = max(1, round(fps / sample_fps))
        index = 0
        while True:
            if not self._cap.grab():
                return
            if index % step == 0:
                ok, frame = self._cap.retrieve()
                if ok:
                    offset = index / fps
                    yield datetime.fromtimestamp(self.start.timestamp() + offset), frame
            index += 1

    def close(self) -> None:
        self._cap.release()


def open_source(
    source: str | int, start: datetime | None = None, password: str | None = None
) -> LiveSource | FileSource:
    if is_file_source(source):
        return FileSource(str(source), start)
    return LiveSource(resolve_password(source, password), label=source)


_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def preprocess(frame: Frame, rotate: int = 0, roi: list[float] | None = None) -> Frame:
    if rotate:
        if rotate not in _ROTATIONS:
            raise ValueError(t("err.rotate"))
        frame = cv2.rotate(frame, _ROTATIONS[rotate])
    if roi:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = roi
        frame = frame[int(y1 * h) : int(y2 * h), int(x1 * w) : int(x2 * w)]
    return frame
