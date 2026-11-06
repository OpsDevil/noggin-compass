from datetime import datetime, timedelta

import numpy as np
import pytest

from noggin_compass.climate import OFF, ClimateWatch, Siren
from noggin_compass.config import (
    ClimateLimits,
    Config,
    Thermometer,
    ThermometersConfig,
    load_config,
    save_thermometers,
)
from noggin_compass.storage import Storage
from noggin_compass.telegram import Reply, TelegramBot
from noggin_compass.thermometer import Found, Reading, battery_percent, climate_lines, menu, tip_lines

CRIB = "5E1A7C30-9B2D-4F86-A1C4-3D7E9F2B6A18"
ROOM = "C9047B2E-61D3-4A5F-8E2B-07F4D1A93C5D"
START = datetime(2026, 11, 7, 2, 0)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def wait(self, minutes):
        self.now += timedelta(minutes=minutes)


class Night:
    """A ClimateWatch with a fake clock that records what it sends."""

    def __init__(self, *devices, **settings):
        self.clock = Clock()
        self.sent = []
        self.notes = []
        cfg = ThermometersConfig(devices=list(devices) or [Thermometer(CRIB, "Crib")])
        for key, value in settings.items():
            setattr(cfg, key, value)
        self.cfg = cfg
        self.watch = ClimateWatch(cfg, lambda text, silent: self.sent.append((text, silent)), self.notes.append,
                                  self.clock)

    def read(self, temperature, humidity=50, battery=2.9, device=None):
        device = device or self.cfg.devices[0]
        self.watch.reading(device, Reading(self.clock(), temperature, humidity, battery))
        return self.watch.tick()

    def texts(self):
        return [text.text if isinstance(text, Reply) else text for text, _ in self.sent]

    def take(self):
        texts, self.sent = self.texts(), []
        return texts


def test_cold_starts_quietly_and_the_alarm_comes_when_still_below_the_limit():
    night = Night()
    assert night.read(21.0) is None and night.take() == []
    assert night.watch.poll_minutes() == 10
    assert night.read(17.8) == "quiet"
    assert night.take() == ["Crib: 17.8 °C, colder than 18 °C. Check the windows and the door. "
                            "I will check again in 10 min: if it is still below 18 °C, the alarm goes off."]
    assert night.watch.poll_minutes() == 2  # follows the temperature closely now
    night.clock.wait(4)
    assert night.read(17.6) == "quiet"
    night.clock.wait(6)
    assert night.watch.tick() == "quiet"  # 10 min are up, but no new reading yet: nothing decided
    night.clock.wait(1)
    assert night.read(17.7) == "alarm"  # the new reading decides
    [(alarm, silent)] = night.sent
    assert not silent and alarm.text.startswith("Alarm, it is cold! Crib: 17.7 °C, below 18 °C.")
    assert alarm.buttons == [[("Turn off", OFF)], [("Mute for 2 h", "climate:mute:2")]]
    assert night.notes == [alarm.text]  # and a notification on the computer, once


def test_the_alarm_repeats_until_it_is_turned_off_and_comes_back_if_still_cold():
    night = Night()
    night.read(17.5)
    night.clock.wait(10)
    assert night.read(17.4) == "alarm"
    night.take()
    night.clock.wait(0.5)
    assert night.watch.tick() == "alarm" and night.take() == []
    night.clock.wait(0.5)
    assert night.watch.tick() == "alarm" and len(night.take()) == 1  # every repeat_minutes, 1 by default
    answer = night.watch.press(OFF)
    assert answer.text == "The alarm is off. I will check again in 20 min: if it is still cold, it goes off again." and not answer.buttons
    assert night.watch.tick() is None  # silent at once
    night.clock.wait(10)
    assert night.read(17.9) is None  # warmer, but still below 18: changes nothing
    night.clock.wait(10)
    assert night.watch.tick() is None  # waits for a new reading
    assert night.read(17.9) == "alarm"
    assert night.take()[0].startswith("Alarm again! Crib: 17.9 °C, still below 18 °C.")
    assert night.watch.press(OFF).text.startswith("The alarm is off")
    assert night.watch.press(OFF).text.startswith("The alarm is off")  # a second phone: still fine


def test_only_the_limit_counts():
    night = Night()
    night.read(15.0)  # a sharp fall
    night.clock.wait(3)
    assert night.read(15.3) == "quiet"  # a little warmer: still cold, nothing changes
    night.clock.wait(6)
    assert night.read(17.9) == "quiet"
    night.clock.wait(1)
    assert night.read(17.9) == "alarm"  # 10 min and still below 18, however it moved meanwhile
    night.take()
    assert night.read(18.0) is None  # at the limit: not cold
    assert night.take() == ["Crib: 18.0 °C, no longer cold. The alarm is over."]
    assert night.read(17.9) == "quiet"  # below again: from the start
    assert night.take()[0].startswith("Crib: 17.9 °C, colder than 18 °C.")
    assert night.read(18.0) is None
    assert night.take() == ["Crib: 18.0 °C, no longer cold."]
    assert night.sent == [] and night.watch.poll_minutes() == 2  # still close to the limit


def test_the_alarm_ends_when_warm_even_after_off():
    night = Night()
    night.read(17.0)
    night.clock.wait(10)
    night.read(17.0)
    night.watch.press(OFF)
    night.take()
    night.clock.wait(5)
    assert night.read(18.5) is None
    assert night.take() == ["Crib: 18.5 °C, no longer cold. The alarm is over."]
    assert night.watch.status_lines() == []


def test_now_says_what_the_alarm_is_doing():
    night = Night()
    night.read(17.0)
    assert night.watch.status_lines() == ["Crib: colder than 18 °C. If it stays below, the alarm goes off."]
    night.clock.wait(10)
    night.read(17.0)
    assert night.watch.status_lines() == ["Crib: cold alarm on."]
    night.watch.press(OFF)
    assert night.watch.status_lines() == ["Crib: the cold alarm is off until 02:30."]


def test_each_thermometer_has_its_own_limits():
    crib = Thermometer(CRIB, "Crib", {"cold_below": 20})
    hall = Thermometer(ROOM, "Hall", {"alarm": False})
    night = Night(crib, hall)
    assert night.read(19.5, device=crib) == "quiet"
    assert night.read(15.0, device=hall) == "quiet"  # the hall makes no alarm
    texts = night.take()
    assert texts[0].startswith("Crib: 19.5 °C, colder than 20 °C.")
    assert texts[1] == ("Hall: 15.0 °C, humidity 50%, colder than advised for sleep (18-22 °C): "
                        "close the windows and the door, dress the baby warmer.")
    assert night.sent == []


def test_tips_come_once_without_a_sound_and_again_hours_later():
    night = Night()
    night.read(23.5, humidity=35)
    assert night.sent == [
        ("Crib: 23.5 °C, humidity 35%, warmer than advised for sleep (18-22 °C): dress the baby lighter, "
         "take off a blanket or turn the heating down.", True),
        ("Crib: 23.5 °C, humidity 35%, drier than advised (40-60% humidity): turn on a humidifier.", True),
    ]
    night.sent = []
    night.clock.wait(10)
    night.read(23.6, humidity=36)
    night.clock.wait(10)
    night.read(21.9, humidity=36)  # the heat is over for a moment
    night.clock.wait(10)
    night.read(22.3, humidity=36)  # back: but too soon to say it again
    assert night.sent == []
    night.clock.wait(3 * 60)
    night.read(21.0, humidity=36)
    night.clock.wait(10)
    night.read(22.5, humidity=36)
    assert night.texts() == ["Crib: 22.5 °C, humidity 36%, warmer than advised for sleep (18-22 °C): "
                             "dress the baby lighter, take off a blanket or turn the heating down."]


def test_after_a_quick_restart_the_same_tip_is_not_sent_again():
    night = Night()
    night.watch.remember({CRIB: Reading(START - timedelta(minutes=5), 23.5, 50, 2.9)})
    night.read(23.4)
    assert night.sent == []
    later = Night()
    later.watch.remember({CRIB: Reading(START - timedelta(hours=5), 23.5, 50, 2.9)})
    later.read(23.4)
    assert len(later.sent) == 1


def test_a_lost_thermometer_is_told_once_and_its_return_too():
    night = Night(Thermometer(CRIB))
    night.read(21.0)
    night.clock.wait(20)
    night.watch.no_answer(night.cfg.devices[0])
    assert night.sent == []
    night.clock.wait(15)
    night.watch.no_answer(night.cfg.devices[0])
    night.clock.wait(10)
    night.watch.no_answer(night.cfg.devices[0])
    assert night.sent == [("The thermometer has not answered for 35 min. Check that it is in place and its battery "
                           "is not flat. Last reading at 02:00: 21.0 °C, humidity 50%.", True)]
    night.sent = []
    night.read(20.5)
    assert night.texts() == ["The thermometer answers again: 20.5 °C, humidity 50%."]


def test_the_alarm_goes_on_while_the_thermometer_is_lost():
    night = Night()
    night.read(17.0)
    night.clock.wait(10)
    night.read(17.0)
    night.clock.wait(40)
    night.watch.no_answer(night.cfg.devices[0])
    assert night.watch.tick() == "alarm"


def test_a_low_battery_is_told_once_a_day():
    night = Night()
    night.read(21.0, battery=2.45)
    night.read(21.0, battery=2.44)
    assert night.sent == [('Thermometer "Crib": the battery is running low, 2.45 V, about 28%. It takes a CR2032.',
                           True)]
    night.clock.wait(25 * 60)
    night.read(21.0, battery=2.40)
    assert len(night.sent) == 2


def test_battery_percent():
    assert [battery_percent(v) for v in (3.2, 3.1, 2.92, 2.84, 2.5, 2.2, 2.0)] == [100, 100, 80, 71, 33, 0, 0]


def test_now_shows_the_battery_and_the_tips():
    devices = [Thermometer(CRIB, "Crib"), Thermometer(ROOM, "Room")]
    now = START
    readings = {CRIB: Reading(now, 23.94, 48, 2.84), ROOM: Reading(now - timedelta(hours=1), 25.0, 30, 2.92)}
    assert climate_lines(devices, readings, now, 10, battery=True) == [
        "Crib: 23.9 °C, humidity 48%, battery 71%.",
        "Room: 25.0 °C, humidity 30%, battery 80% (1 h 00 min ago).",
    ]
    # No tips from an hour-old reading: it may be over.
    assert tip_lines(devices, readings, ClimateLimits(), now, 10) == [
        "Crib: warmer than advised for sleep (18-22 °C): dress the baby lighter, take off a blanket or turn the heating down."
    ]
    one = tip_lines([Thermometer(CRIB)], readings, ClimateLimits(), now, 10)
    assert one[0].startswith("Warmer than advised for sleep")


def test_the_siren_beeps_every_few_seconds_and_then_sounds_with_no_pause():
    import time as clock

    class Watch:
        sound = "quiet"

        def tick(self):
            return self.sound

    played, waits = [], []

    def play(sound, volume):
        played.append((sound, volume))
        if sound == "siren":
            clock.sleep(0.25)  # the siren takes a while to play

    def wait(seconds):  # instead of the pause: stop after 3 siren rounds or 20 waits
        waits.append(seconds)
        return sum(s == "siren" for s, _ in played) >= 3 or len(waits) >= 20

    watch = Watch()
    siren = Siren(watch, ThermometersConfig().cold, sound=play)
    siren._stop.wait = wait
    siren._stop.is_set = lambda: sum(s == "siren" for s, _ in played) >= 3 or len(waits) >= 20
    siren._run()
    assert played[0] == ("beep", 1.0) and len(played) == 1  # waits are not real here: one beep
    played.clear()
    waits.clear()
    watch.sound = "alarm"
    siren._run()
    assert played == [("siren", 1.0)] * 3 and waits == []  # back to back, no pause between them


@pytest.mark.parametrize("name, seconds", [("beep", (0.3, 0.8)), ("siren", (2.5, 3.5))])
def test_the_alarm_sounds_are_made_by_the_program(tmp_path, monkeypatch, name, seconds):
    import tempfile
    import wave

    from noggin_compass import notify

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(notify, "_made", {})
    (tmp_path / f"noggin-compass-{name}.wav").write_bytes(b"left by an older version")
    path = notify.sound_path(name)
    with wave.open(path) as sound:
        frames = np.frombuffer(sound.readframes(sound.getnframes()), dtype=np.int16)
        assert sound.getframerate() == 44100 and seconds[0] < sound.getnframes() / 44100 < seconds[1]
    assert np.abs(frames).max() > 30000  # at full scale: as loud as the Mac's volume allows


def test_config_takes_limits_per_thermometer_and_keeps_them(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(f"thermometers:\n  devices:\n    - address: {CRIB}\n      name: Crib\n      cold_below: 20\n"
                      f"    - address: {ROOM}\n      alarm: false\n  limits:\n    hot_above: 24\n")
    cfg = load_config(config)
    crib, room = cfg.thermometers.devices
    assert crib.limits_from(cfg.thermometers.limits) == ClimateLimits(cold_below=20, hot_above=24)
    assert room.limits_from(cfg.thermometers.limits) == ClimateLimits(hot_above=24, alarm=False)
    save_thermometers(config, [room, crib])
    assert load_config(config).thermometers.devices == [room, crib]
    for wrong in ("alarm: 18", "cold_below: yes", "cold_below: cold"):
        config.write_text(f"thermometers:\n  devices:\n    - address: {CRIB}\n      {wrong}\n")
        with pytest.raises(ValueError, match="thermometers.devices.1"):
            load_config(config)


def test_the_menu_keeps_a_thermometers_limits_and_saves_its_readings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("language: en\n")
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    cfg.thermometers.devices = [Thermometer(CRIB, "Crib", {"cold_below": 20})]
    taken = Reading(START, 21.5, 45, 2.9)

    async def look(seconds):
        return [(Found(CRIB, "LYWSD03MMC", -60), taken, None), (Found(ROOM, "LYWSD03MMC", -70), taken, None)]

    answers = iter(["1 2", "", "Room"])
    result = menu(cfg, config, 30, ask=lambda _: next(answers), out=lambda _: None, look=look)
    assert result == [Thermometer(CRIB, "Crib", {"cold_below": 20}), Thermometer(ROOM, "Room")]
    storage = Storage(cfg.storage.db_path)
    assert storage.latest_climate() == {CRIB: taken, ROOM: taken}
    storage.close()


def test_telegram_can_send_without_a_sound():
    calls = []
    bot = TelegramBot("token", [1], lambda text: None)
    bot.call = lambda method, **params: calls.append(params)
    bot.send("tip", silent=True)
    bot.send(Reply("alarm", [[("Turn off", OFF)]]))
    assert calls[0]["disable_notification"] == "true" and "disable_notification" not in calls[1]


def test_without_a_new_reading_nothing_is_decided():
    night = Night()
    night.read(17.5)
    night.take()
    night.clock.wait(60)  # the thermometer is silent
    assert night.watch.tick() == "quiet"
    assert night.take() == []


def test_after_off_the_first_cold_reading_brings_the_alarm_back():
    night = Night()
    night.read(17.5)
    night.clock.wait(10)
    night.read(17.5)
    night.watch.press(OFF)
    night.clock.wait(19)
    assert night.read(17.6) is None  # before the 20 min: still off
    night.clock.wait(3)
    assert night.watch.tick() is None  # 20 min are up, no new reading yet
    assert night.read(17.6) == "alarm"


def test_one_failed_message_does_not_stop_the_others():
    sent = []

    def broken(text):
        raise OSError("osascript is gone")

    night = Night()
    night.watch.notify = broken
    night.watch.send = lambda text, silent: sent.append(text)
    night.read(17.5)
    night.clock.wait(10)
    assert night.read(17.5) == "alarm"
    assert any(text.text.startswith("Alarm, it is cold!") for text in sent if isinstance(text, Reply))


def test_messages_go_out_from_their_own_thread():
    import threading

    from noggin_compass.notify import Outbox

    got = threading.Event()
    threads = []

    def send(text, silent):
        threads.append(threading.current_thread().name)
        got.set()

    outbox = Outbox()
    outbox.start()
    night = Night()
    night.watch.send = lambda text, silent: outbox.put(send, text, silent)
    night.read(17.5)
    assert got.wait(2) and threads == ["outbox"]
    outbox.stop()


def test_a_one_line_thermometers_section_is_left_alone(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("thermometers: {every_minutes: 5}\n")
    with pytest.raises(ValueError, match="by hand"):
        save_thermometers(config, [Thermometer(CRIB)])
    assert config.read_text() == "thermometers: {every_minutes: 5}\n"


def test_one_search_reads_each_as_seen_and_one_seen_late_still_gets_read(monkeypatch):
    import asyncio
    import sys
    import types

    from noggin_compass import thermometer as thermometer_module

    class Device:
        def __init__(self, address):
            self.address = address

    class Scanner:
        def __init__(self, detection_callback):
            self.callback = detection_callback

        async def __aenter__(self):
            loop = asyncio.get_running_loop()
            loop.call_soon(self.callback, Device(ROOM.lower()), None)  # case does not matter
            loop.call_later(0.05, self.callback, Device(CRIB), None)  # seen during the slow first read
            return self

        async def __aexit__(self, *exc):
            return False

    async def slow_read(device, scan_seconds):
        await asyncio.sleep(0.2)  # longer than the whole search
        return Reading(START, 20.0, 50, 2.9)

    monkeypatch.setitem(sys.modules, "bleak", types.SimpleNamespace(BleakScanner=Scanner))
    monkeypatch.setattr(thermometer_module, "read", slow_read)

    async def collect():
        return [(address, result) async for address, result in
                thermometer_module.read_all([CRIB, ROOM, "AAAA"], scan_seconds=0.1)]

    results = asyncio.run(collect())
    assert [address for address, _ in results] == [ROOM, CRIB, "AAAA"]
    assert isinstance(results[0][1], Reading) and isinstance(results[1][1], Reading)
    assert isinstance(results[2][1], LookupError)


def test_mute_stops_the_cold_alarm_for_hours_and_it_starts_over_after():
    night = Night()
    night.read(17.5)
    night.clock.wait(10)
    assert night.read(17.5) == "alarm"
    night.take()
    answer = night.watch.press("climate:mute:2")  # the button under the alarm
    assert answer.text.startswith("The cold alarm is muted until 04:10")
    assert [label for label, _ in answer.buttons[0]] == ["1 h", "2 h", "3 h", "4 h"]
    assert answer.buttons[1] == [("Turn it on", "climate:unmute")]
    assert night.watch.tick() is None  # silent at once
    night.clock.wait(30)
    assert night.read(16.0) is None and night.take() == []  # the window is open: nothing
    assert night.watch.status_lines() == ["The cold alarm is muted until 04:10."]
    assert night.watch.poll_minutes() == 10  # no need to follow it closely meanwhile
    night.clock.wait(90)
    assert night.watch.tick() is None
    assert night.take() == ["The cold alarm is on again."]
    assert night.read(17.0) == "quiet"  # still cold: from the start, quietly
    assert night.take()[0].startswith("Crib: 17.0 °C, colder than 18 °C.")


def test_unmute_brings_the_cold_alarm_back_at_once():
    night = Night()
    night.watch.mute(2)
    night.read(17.0)
    assert night.take() == []
    answer = night.watch.press("climate:unmute")
    assert answer.text == "The cold alarm is on." and answer.buttons == [[("Mute for 2 h", "climate:mute:2")]]
    assert night.read(17.0) == "quiet"
