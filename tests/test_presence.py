import logging

import numpy as np
import pytest

from noggin_compass import presence as presence_module
from noggin_compass.cli import analyze
from noggin_compass.config import Config
from noggin_compass.head import Side
from noggin_compass.pose import Person
from noggin_compass.presence import BABY, EMPTY, PresenceCheck, start, torch_device

FRAME = np.zeros((120, 160, 3), dtype=np.uint8)


class ScriptedPresence(PresenceCheck):
    """PresenceCheck without the network: score() returns what the test says, or raises it."""

    def __init__(self, scores, threshold=0.35, every_seconds=5.0):
        self.threshold = threshold
        self.every_seconds = every_seconds
        self._last = None
        self._failing = False
        self.scores = list(scores)
        self.calls = 0

    def score(self, frame):
        self.calls += 1
        result = self.scores.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_answer_holds_between_checks():
    presence = ScriptedPresence([0.9, 0.1])
    assert presence.check(FRAME, now=0.0) is True
    assert presence.check(FRAME, now=4.0) is True  # not looked again yet
    assert presence.calls == 1
    assert presence.check(FRAME, now=5.0) is False
    assert presence.calls == 2


def test_threshold_decides():
    assert ScriptedPresence([0.35]).check(FRAME, now=0.0) is True
    assert ScriptedPresence([0.34]).check(FRAME, now=0.0) is False


def test_both_sides_have_descriptions():
    assert BABY and EMPTY and not set(BABY) & set(EMPTY)


class ScriptedDetector:
    def __init__(self, person):
        self.person = person
        self.calls = 0

    def detect_upright(self, frame, min_conf, now=None):
        self.calls += 1
        return self.person

    def detect(self, frame):
        self.calls += 1
        return self.person


def baby_facing_up() -> Person:
    xy = np.zeros((17, 2))
    conf = np.zeros(17)
    xy[:7] = [(80, 30), (75, 27), (85, 27), (70, 30), (90, 30), (65, 60), (95, 60)]
    conf[:7] = 0.9
    return Person(np.array([60.0, 20.0, 100.0, 110.0]), 0.8, xy, conf)


def test_empty_crib_skips_the_pose_model():
    detector = ScriptedDetector(baby_facing_up())  # a "person" in the sheet print
    estimate, person = analyze(detector, FRAME, Config(), 0.0, ScriptedPresence([0.05]))
    assert estimate.side == Side.ABSENT and estimate.method == "crib_empty"
    assert person is None and detector.calls == 0


def test_baby_the_pose_model_lost_is_not_visible_rather_than_gone():
    estimate, person = analyze(ScriptedDetector(None), FRAME, Config(), 0.0, ScriptedPresence([0.9]))
    assert estimate.side == Side.UNKNOWN and estimate.method == "baby_unseen"
    assert person is None


def test_baby_in_the_crib_gets_the_head_side_from_the_pose_model():
    detector = ScriptedDetector(baby_facing_up())
    estimate, person = analyze(detector, FRAME, Config(), 0.0, ScriptedPresence([0.9]))
    assert estimate.side == Side.CENTER and person is not None


@pytest.mark.parametrize("found", [None, baby_facing_up()])
def test_without_the_presence_check_the_pose_model_decides(found):
    estimate, _ = analyze(ScriptedDetector(found), FRAME, Config(), 0.0, None)
    assert estimate.side == (Side.ABSENT if found is None else Side.CENTER)


def test_last_score_is_kept_for_the_snapshots():
    presence = ScriptedPresence([0.62])
    assert presence.last_score is None
    presence.check(FRAME, now=0.0)
    assert presence.last_score == 0.62


@pytest.mark.parametrize(
    ("device", "expected"),
    [("cpu", "cpu"), ("mps", "mps"), ("0", "cuda:0"), (0, "cuda:0"), ("1,0", "cuda:1"), ("cuda:2", "cuda:2")],
)
def test_ultralytics_device_names_become_torch_devices(device, expected):
    assert torch_device(device) == expected


def test_cut_off_download_is_fetched_again(monkeypatch):
    transformers = pytest.importorskip("transformers")
    calls = []

    def fake(kind):
        def from_pretrained(model, cache_dir, local_files_only):
            calls.append((kind, local_files_only))
            if local_files_only:
                raise OSError("does not appear to have a file named model.safetensors")
            return kind
        return from_pretrained

    monkeypatch.setattr(transformers.AutoModel, "from_pretrained", fake("model"))
    monkeypatch.setattr(transformers.AutoProcessor, "from_pretrained", fake("processor"))
    assert presence_module._load("google/siglip2-base-patch16-224") == ("model", "processor")
    assert calls == [("model", True), ("model", False), ("processor", False)]


def test_a_failing_network_gives_no_answer_and_is_tried_again(caplog):
    caplog.set_level(logging.INFO, logger="noggin_compass")
    presence = ScriptedPresence([0.9, RuntimeError("MPS backend out of memory"), RuntimeError("still"), 0.05])
    assert presence.check(FRAME, now=0.0) is True
    # A failure drops the old "in the crib" too: the baby may have been taken out since, so the
    # pose model decides and the snapshot gets no score rather than a stale one.
    assert presence.check(FRAME, now=5.0) is None and presence.last_score is None
    assert presence.check(FRAME, now=9.0) is None and presence.calls == 2  # not every frame
    assert presence.check(FRAME, now=10.0) is None and presence.calls == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "MPS backend out of memory" in warnings[0].getMessage()
    assert presence.check(FRAME, now=15.0) is False
    assert "works again" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("found", [None, baby_facing_up()])
def test_while_the_network_fails_the_pose_model_decides(found):
    detector = ScriptedDetector(found)
    estimate, _ = analyze(detector, FRAME, Config(), 0.0, ScriptedPresence([RuntimeError("broken")]))
    assert detector.calls == 1
    if found is None:
        assert estimate.side == Side.ABSENT and estimate.method == "no_person"
    else:
        assert estimate.side == Side.CENTER


def test_a_model_that_cannot_be_loaded_leaves_the_pose_model_alone(monkeypatch, caplog):
    def no_internet(*args):
        raise OSError("We couldn't connect to 'https://huggingface.co'")

    monkeypatch.setattr(presence_module, "PresenceCheck", no_internet)
    assert start("google/siglip2-base-patch16-224", "cpu", 0.35, 5.0) is None
    message = caplog.records[-1].getMessage()
    assert caplog.records[-1].levelno == logging.WARNING
    assert "google/siglip2-base-patch16-224" in message and "OSError" in message


def test_a_model_that_loads_is_used(monkeypatch):
    monkeypatch.setattr(presence_module, "PresenceCheck", lambda *args: ("loaded", args))
    assert start("m", "mps", 0.4, 3.0) == ("loaded", ("m", "mps", 0.4, 3.0))
