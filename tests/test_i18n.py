import string
from datetime import datetime, time, timedelta

import pytest

from noggin_compass.head import HeadEstimate, Side, side_label
from noggin_compass.i18n import MESSAGES, set_language, t
from noggin_compass.report import build_report, fmt_duration, render


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_language_has_the_same_keys_and_placeholders():
    english = MESSAGES["en"]
    for language, messages in MESSAGES.items():
        assert messages.keys() == english.keys(), language
        for key, text in messages.items():
            assert placeholders(text) == placeholders(english[key]), (language, key)


def test_switching_language():
    assert side_label(Side.LEFT) == "left"
    set_language("ru")
    assert side_label(Side.LEFT) == "влево"
    assert fmt_duration(3900) == "1 ч 05 мин"
    estimate = HeadEstimate(Side.LEFT, 0.9, method="both_ears", axis_source="shoulders")
    assert estimate.describe() == "оба уха, ось: плечи"
    assert estimate.code == "both_ears/shoulders"


def test_unknown_language_is_rejected():
    with pytest.raises(ValueError):
        set_language("de")


def test_russian_report():
    set_language("ru")
    noon = time(12, 0)
    start = datetime(2026, 10, 29, 22, 0)
    samples = [(start + timedelta(seconds=i), Side.LEFT, 0.6) for i in range(600)]
    text = render(build_report(samples, center_threshold=0.35, confirm_seconds=10, gap_seconds=60,
                               day_start=noon), noon)
    assert "Сутки 29.10.2026:" in text and "10 мин" in text and "Л" in text
    assert t("report.no_data") in render([], noon)
