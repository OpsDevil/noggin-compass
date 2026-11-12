import dataclasses
from pathlib import Path

import pytest
import yaml

from noggin_compass.config import Config, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def _keys(obj, prefix=""):
    for field in dataclasses.fields(obj):
        value = getattr(obj, field.name)
        if dataclasses.is_dataclass(value):
            yield from _keys(value, f"{prefix}{field.name}.")
        else:
            yield f"{prefix}{field.name}"


def _yaml_keys(data, prefix=""):
    for key, value in data.items():
        if isinstance(value, dict):
            yield from _yaml_keys(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}"


def test_example_config_documents_every_option_with_its_default():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert set(_yaml_keys(data)) == set(_keys(Config()))
    assert load_config(EXAMPLE) == Config()


def test_unknown_option_names_itself(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("detector:\n  min_face_size: 0.015\n")
    with pytest.raises(ValueError, match="detector.min_face_size"):
        load_config(config)


@pytest.mark.parametrize("option", ["detector.upright_margin", "head.half_head_to_eye_dist",
                                    "motion.width", "sleep.min_frames"])
def test_a_divisor_set_to_zero_is_refused_at_the_start(tmp_path, option):
    section, name = option.split(".")
    config = tmp_path / "config.yaml"
    config.write_text(f"{section}:\n  {name}: 0\n")
    with pytest.raises(ValueError, match=option):
        load_config(config)
