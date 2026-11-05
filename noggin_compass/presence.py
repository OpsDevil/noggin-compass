"""Is the baby in the crib at all? A second network answers that from the whole picture.

The pose model looks for the joints of people. To find a baby in a sleep sack its person threshold
has to be low, and then it also finds "people" in an empty crib: in the sheet print, in a toy with
a face, in clothes laid out, in an empty sleep sack. A model that compares the picture with short
descriptions, "a baby in a crib" against "an empty crib", tells these apart easily, because it
judges the whole scene rather than a few points.

SigLIP 2 (Google, Apache-2.0) does this without any training. It runs every few seconds, and the
pose model still decides where the head is turned. Any fixed-resolution SigLIP or SigLIP 2 model
works (not the NaFlex ones, which take pictures of any shape).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import cv2
import numpy as np

from .i18n import t

MODELS_DIR = Path("models") / "hf"
log = logging.getLogger("noggin_compass")

# Several wordings of each side, averaged by the softmax below. They name the usual look-alikes
# (a blanket, clothes, an empty sleep sack, a toy, infrared night vision) in general words, so the
# empty side has a description for each of them.
BABY = (
    "an overhead camera view of a baby in a crib",
    "a baby monitor image of a sleeping infant",
    "a photo of a baby lying on its back in a crib",
    "a photo of a baby lying on its tummy in a crib",
    "a sleeping baby covered with a blanket",
    "a baby wearing a sleep sack",
    "a top-down view of an infant on a mattress",
    "a dim photo of a baby sleeping in a crib at night",
)
EMPTY = (
    "an overhead camera view of an empty crib",
    "a baby monitor image of an empty crib",
    "an empty crib with a patterned sheet",
    "an empty mattress with no one on it",
    "a blanket lying in an empty crib",
    "clothes laid out on an empty mattress",
    "an empty sleeping bag lying in a crib",
    "a stuffed toy in an empty crib",
    "a dim photo of an empty crib at night",
    "a black and white infrared image of an empty crib",
)


class PresenceCheck:
    def __init__(self, model: str, device: str = "cpu", threshold: float = 0.35, every_seconds: float = 5.0):
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        import torch
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        self.torch = torch
        self.device = torch_device(device)
        self.threshold = threshold
        self.every_seconds = every_seconds
        self.model, self.processor = _load(model)
        self.model = self.model.to(self.device).eval()
        size = self.processor.image_processor.size
        self.size = (size["width"], size["height"])
        with torch.no_grad():
            text = self.processor(text=list(BABY + EMPTY), padding="max_length", max_length=64, return_tensors="pt")
            self.text = self._unit(self.model.get_text_features(**text.to(self.device)))
        self.scale = float(self.model.logit_scale.detach().exp())
        self._last: tuple[float, float] | None = None  # time and score of the last check

    def _unit(self, features) -> object:
        features = features if self.torch.is_tensor(features) else features.pooler_output
        return self.torch.nn.functional.normalize(features, dim=-1)

    def score(self, frame: np.ndarray) -> float:
        """How sure the network is that the frame shows a baby rather than an empty crib, 0..1."""
        from PIL import Image

        # The whole crib squeezed into the model's square input: a crop could cut the baby off.
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(self.size, Image.BICUBIC)
        with self.torch.no_grad():
            pixels = self.processor(images=[image], return_tensors="pt")["pixel_values"].to(self.device)
            logits = self.scale * self._unit(self.model.get_image_features(pixel_values=pixels)) @ self.text.T
        return float(logits.softmax(-1)[0, : len(BABY)].sum())

    def check(self, frame: np.ndarray, now: float | None = None) -> bool:
        """Whether the baby is in the crib. Checked every every_seconds; in between the last answer
        holds, since a baby is not put in or taken out faster than that."""
        if self._last is None or now is None or now - self._last[0] >= self.every_seconds:
            self._last = (now or 0.0, self.score(frame))
        return self._last[1] >= self.threshold

    @property
    def last_score(self) -> float | None:
        """The score behind the current answer, saved with the snapshots to tune the threshold."""
        return None if self._last is None else self._last[1]


def torch_device(device: str | int) -> str:
    """detector.device in the Ultralytics form ("0" or "0,1" for NVIDIA cards) as a torch device."""
    first = str(device).split(",")[0].strip()
    return f"cuda:{first}" if first.isdigit() else first


def _load(model: str):
    """The model and its processor from models/. Downloaded there if missing or incomplete (an
    earlier download was cut off), after that nothing is requested from the internet."""
    from transformers import AutoModel, AutoProcessor
    from transformers.utils import logging as hf_logging

    def load(local: bool):
        return (
            AutoModel.from_pretrained(model, cache_dir=MODELS_DIR, local_files_only=local),
            AutoProcessor.from_pretrained(model, cache_dir=MODELS_DIR, local_files_only=local),
        )

    hf_logging.disable_progress_bar()
    try:
        return load(local=True)
    except OSError:
        log.info(t("presence.downloading", model=model, path=MODELS_DIR))
        hf_logging.enable_progress_bar()
        try:
            return load(local=False)
        finally:
            hf_logging.disable_progress_bar()
