import asyncio
import logging
from datetime import datetime, timedelta

import numpy as np
import pytest

from noggin_compass import thermometer as thermometer_module
from noggin_compass.config import Config, Thermometer, load_config, save_thermometers
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.preview import draw
from noggin_compass.storage import Storage
from noggin_compass.thermometer import Found, Reading, ThermometerPoller, climate_lines, menu, parse

CRIB = "5E1A7C30-9B2D-4F86-A1C4-3D7E9F2B6A18"
ROOM = "C9047B2E-61D3-4A5F-8E2B-07F4D1A93C5D"
NOW = datetime(2026, 11, 6, 8, 30)


def reading(temperature=23.94, humidity=50, battery=2.854, minutes_ago=1):
    return Reading(NOW - timedelta(minutes=minutes_ago), temperature, humidity, battery)


def test_the_five_bytes_the_thermometer_sends():
    # 23.94 C, 50%, 2854 mV, as read from the real one on 2026-11-06.
    data = (2394).to_bytes(2, "little", signed=True) + bytes([50]) + (2854).to_bytes(2, "little")
    assert parse(data, NOW) == Reading(NOW, 23.94, 50, 2.854)
    below_zero = (-150).to_bytes(2, "little", signed=True) + bytes([80]) + (3000).to_bytes(2, "little")
    assert parse(below_zero, NOW).temperature == -1.5


def test_one_thermometer_is_shown_without_its_name():
    lines = climate_lines([Thermometer(CRIB, "Crib")], {CRIB: reading()}, NOW, 10)
    assert lines == ["Temperature 23.9 °C, humidity 50%."]


def test_several_thermometers_are_shown_by_name():
    devices = [Thermometer(CRIB, "Crib"), Thermometer(ROOM, "")]
    lines = climate_lines(devices, {CRIB: reading(), ROOM: reading(24.7, 44)}, NOW, 10)
    assert lines == ["Crib: 23.9 °C, humidity 50%.", "3C5D: 24.7 °C, humidity 44%."]


def test_an_old_reading_says_how_old_and_a_missing_one_says_so():
    devices = [Thermometer(CRIB, "Crib"), Thermometer(ROOM, "Room")]
    lines = climate_lines(devices, {CRIB: reading(minutes_ago=45)}, NOW, 10)
    assert lines == ["Crib: 23.9 °C, humidity 50% (45 min ago).", "Room: no readings yet."]
    assert climate_lines([Thermometer(CRIB)], {}, NOW, 10) == ["No readings from the thermometer yet."]


def test_a_low_battery_is_mentioned():
    lines = climate_lines([Thermometer(CRIB)], {CRIB: reading(battery=2.41)}, NOW, 10, low_battery_volts=2.5)
    assert lines[-1] == "The battery in the thermometer is running low."
    assert len(climate_lines([Thermometer(CRIB)], {CRIB: reading(battery=2.9)}, NOW, 10, 2.5)) == 1


def test_readings_are_kept_per_thermometer(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    storage.add_climate(CRIB, reading(22.0, minutes_ago=20))
    storage.add_climate(CRIB, reading(23.0, minutes_ago=10))
    storage.add_climate(ROOM, reading(25.0, minutes_ago=15))
    latest = storage.latest_climate()
    storage.close()
    assert latest[CRIB].temperature == 23.0 and latest[CRIB].time == NOW - timedelta(minutes=10)
    assert latest[ROOM].temperature == 25.0 and latest[ROOM].humidity == 50


def test_config_lists_thermometers(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"thermometers:\n  devices:\n    - address: {CRIB}\n      name: Crib\n    - address: {ROOM}\n")
    assert load_config(config).thermometers.devices == [Thermometer(CRIB, "Crib"), Thermometer(ROOM, "")]
    config.write_text("thermometers:\n  devices:\n    - name: Crib\n")
    with pytest.raises(ValueError, match="address"):
        load_config(config)
    config.write_text(f"thermometers:\n  devices:\n    - address: {CRIB}\n      room: crib\n")
    with pytest.raises(ValueError, match="thermometers.devices.1.room"):
        load_config(config)


EXAMPLE_LIKE = """\
language: ru  # interface language

thermometers:
  # chosen with noggin-compass thermometer
  devices: []
  every_minutes: 5          # read often

storage:
  db_path: data/x.db
"""


def test_saving_the_choice_keeps_the_rest_of_the_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(EXAMPLE_LIKE)
    save_thermometers(config, [Thermometer(CRIB, "Кроватка"), Thermometer(ROOM, 'Room "2"')])
    text = config.read_text()
    assert "language: ru  # interface language" in text and "# chosen with noggin-compass thermometer" in text
    assert "every_minutes: 5          # read often" in text and "db_path: data/x.db" in text
    loaded = load_config(config)
    assert loaded.thermometers.devices == [Thermometer(CRIB, "Кроватка"), Thermometer(ROOM, 'Room "2"')]
    assert loaded.thermometers.every_minutes == 5

    save_thermometers(config, [Thermometer(ROOM)])  # the old list is replaced, not added to
    assert load_config(config).thermometers.devices == [Thermometer(ROOM)]
    save_thermometers(config, [])
    assert load_config(config).thermometers.devices == [] and load_config(config).thermometers.every_minutes == 5


def test_saving_adds_the_section_when_there_is_none(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("language: en\n")
    save_thermometers(config, [Thermometer(CRIB)])
    assert load_config(config).thermometers.devices == [Thermometer(CRIB)]
    config.write_text("thermometers:  # mine\n  every_minutes: 20\n")
    save_thermometers(config, [Thermometer(CRIB, "Crib")])
    loaded = load_config(config)
    assert loaded.thermometers.devices == [Thermometer(CRIB, "Crib")] and loaded.thermometers.every_minutes == 20


class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        return self.answers.pop(0)


def nearby(*rows):
    async def look(seconds):
        return list(rows)
    return look


def run_menu(tmp_path, answers, rows, devices=()):
    config = tmp_path / "config.yaml"
    config.write_text("language: en\n")
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    cfg.thermometers.devices = list(devices)
    printed = []
    result = menu(cfg, config, 30, ask=answers, out=printed.append, look=nearby(*rows))
    return result, printed, config


def test_menu_shows_the_thermometers_nearby_and_saves_the_one_chosen(tmp_path):
    rows = [(Found(CRIB, "LYWSD03MMC", -71), reading(), None),
            (Found(ROOM, "LYWSD03MMC", -88), None, "BleakError: busy")]
    result, printed, config = run_menu(tmp_path, Answers("1"), rows)
    assert "  1. 23.9 °C, humidity 50%, signal fair, battery 2.85 V  (id 6A18)" in printed
    assert "  2. no reading (BleakError: busy)  (id 3C5D)" in printed
    assert result == [Thermometer(CRIB, "")]  # one thermometer: no name asked
    assert load_config(config).thermometers.devices == [Thermometer(CRIB, "")]


def test_menu_asks_names_when_several_are_chosen(tmp_path):
    rows = [(Found(CRIB, "LYWSD03MMC", -60), reading(), None), (Found(ROOM, "LYWSD03MMC", -75), reading(24.7), None)]
    answers = Answers("abc", "1 3", "2, 1", "Room", "")
    result, printed, _ = run_menu(tmp_path, answers, rows, devices=[Thermometer(CRIB, "Crib")])
    assert printed.count("There is no thermometer abc in the list, enter numbers from it.") == 1
    assert "There is no thermometer 1 3 in the list, enter numbers from it." in printed
    assert "  1. 23.9 °C, humidity 50%, signal good, battery 2.85 V  (id 6A18) - chosen as \"Crib\"" in printed
    assert result == [Thermometer(ROOM, "Room"), Thermometer(CRIB, "Crib")]  # Enter keeps the old name
    assert "[Crib]" in answers.questions[-1]


def test_menu_keeps_a_chosen_thermometer_that_is_away_and_can_drop_all(tmp_path):
    result, printed, config = run_menu(tmp_path, Answers("0"), [], devices=[Thermometer(CRIB, "Crib")])
    assert "  1. not found nearby  (id 6A18) - chosen as \"Crib\"" in printed
    assert result == [] and load_config(config).thermometers.devices == []


def test_menu_enter_changes_nothing(tmp_path):
    rows = [(Found(CRIB, "LYWSD03MMC", -60), reading(), None)]
    result, printed, config = run_menu(tmp_path, Answers(""), rows)
    assert result is None and printed[-1] == "Nothing changed." and config.read_text() == "language: en\n"
    result, printed, _ = run_menu(tmp_path, Answers(), [])
    assert result is None and "No thermometers found" in printed[-1]


def test_a_thermometer_that_does_not_answer_is_logged_once(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="noggin_compass")
    polls = [[(CRIB, LookupError("not seen in 60 s"))], [(CRIB, LookupError("again"))], [(CRIB, reading(21.5))]]

    async def fake_read_all(addresses, scan_seconds):
        for result in polls.pop(0):
            yield result

    monkeypatch.setattr(thermometer_module, "read_all", fake_read_all)
    told = []

    class Watch:
        def reading(self, device, value):
            told.append(("reading", device.name, value.temperature))

        def no_answer(self, device):
            told.append(("no_answer", device.name))

    poller = ThermometerPoller([Thermometer(CRIB, "Crib")], str(tmp_path / "db.sqlite"), 10, 60, Watch())
    storage = Storage(tmp_path / "db.sqlite")
    for _ in range(3):
        asyncio.run(poller._read_all(storage))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ['Thermometer "Crib" did not answer (LookupError: not seen in 60 s). Trying again every 10 min']
    assert 'Thermometer "Crib" answers again' in caplog.text
    assert storage.latest_climate()[CRIB].temperature == 21.5
    assert told == [("no_answer", "Crib"), ("no_answer", "Crib"), ("reading", "Crib", 21.5)]
    storage.close()


def test_bluetooth_off_counts_as_no_answer_from_each(tmp_path, monkeypatch):
    async def broken(addresses, scan_seconds):
        raise OSError("Bluetooth is off")
        yield  # an async generator, like the real one

    monkeypatch.setattr(thermometer_module, "read_all", broken)
    poller = ThermometerPoller([Thermometer(CRIB), Thermometer(ROOM)], str(tmp_path / "db.sqlite"), 10, 60)
    storage = Storage(tmp_path / "db.sqlite")
    asyncio.run(poller._read_all(storage))
    storage.close()
    assert poller._failing == {CRIB, ROOM}


def test_the_preview_shows_the_readings():
    frame = np.full((300, 400, 3), 90, dtype=np.uint8)
    estimate = HeadEstimate(Side.ABSENT, method="crib_empty")
    plain = draw(frame, None, estimate, 0.4)
    noted = draw(frame, None, estimate, 0.4, notes=["Temperature 23.9 °C, humidity 50%."])
    assert (plain[250:, :200] != noted[250:, :200]).any()  # written in the bottom left corner
    assert (plain[:120] == noted[:120]).all()
