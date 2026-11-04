import dataclasses
from pathlib import Path

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
