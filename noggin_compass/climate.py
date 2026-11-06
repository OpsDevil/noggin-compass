"""The room climate: the cold alarm, tips about the heat and the humidity, and word in Telegram
when a thermometer is lost or its battery runs low.

The cold alarm is for a window or a door left open at night. It starts quietly, so that a
draught that passes by itself wakes nobody, and gets loud only if it does not get warmer. Its
sounds play on the computer running the program, and its messages go to Telegram with an Off
button; Off silences it for a while, and it comes back if it is still not warmer by then.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import ColdAlarmConfig, Thermometer, ThermometersConfig
from .i18n import t
from .report import fmt_duration
from .telegram import Reply
from .thermometer import Reading, battery_percent, label, reading_text, tips

log = logging.getLogger("noggin_compass")

OFF = "climate:off"  # data of the Off button
QUIET_EVERY = 60  # seconds between quiet beeps


@dataclass
class Cold:
    stage: str  # "quiet", "alarm", or "off" for an alarm turned off for a while
    since: datetime  # when the stage began
    reference: float  # the temperature then: "warmer" means above it by warming_degrees
    reason: str = "climate.alarm"  # what the alarm message says
    sent: datetime | None = None  # the last alarm message


class ClimateWatch:
    """Gets each reading from the ThermometerPoller and decides what to say and what to sound.
    `send(text, silent)` posts to Telegram; `notify(text)` shows a notification on the computer.
    Called from the poller's, the bot's and the siren's threads, hence the lock. The messages are
    sent after it is released: Telegram can take seconds to answer."""

    def __init__(
        self,
        cfg: ThermometersConfig,
        send: Callable[[str | Reply, bool], None] | None = None,
        notify: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.cfg = cfg
        self.send = send
        self.notify = notify
        self.clock = clock
        self.cold: dict[str, Cold] = {}
        self.latest: dict[str, Reading] = {}
        self._devices = {device.address: device for device in cfg.devices}
        self._started = clock()
        self._answered: dict[str, datetime] = {}  # last reading in this run
        self._lost: set[str] = set()
        self._battery_told: dict[str, datetime] = {}
        self._tips_now: dict[str, set[str]] = {}
        self._tips_told: dict[tuple[str, str], datetime] = {}
        self._outbox: list[tuple[str | Reply, bool]] = []
        self._notes: list[str] = []  # for notify
        self._lock = threading.Lock()
        self._mail: queue.Queue | None = None  # set by start(): messages then go out in a thread

    def start(self) -> None:
        """Send from a thread of its own from now on. Telegram can hang for half a minute without
        a network, and the siren must not fall silent meanwhile, nor the readings stop."""
        self._mail = queue.Queue()
        threading.Thread(target=self._post, name="climate-messages", daemon=True).start()

    def stop(self) -> None:
        if self._mail is not None:
            self._mail.put(None)

    # Called by the poller.

    def remember(self, readings: dict[str, Reading]) -> None:
        """The readings saved before a restart. A tip that already held a moment ago is not sent
        again, but one from a reading hours old is, as the program was off meanwhile."""
        now = self.clock()
        with self._lock:
            for address, reading in readings.items():
                device = self._devices.get(address)
                if device is None:
                    continue
                self.latest[address] = reading
                if now - reading.time <= timedelta(minutes=2 * self.cfg.every_minutes):
                    self._tips_now[address] = {kind for kind, _ in self._tips(device, reading)}

    def reading(self, device: Thermometer, reading: Reading) -> None:
        with self._lock:
            now = self.clock()
            address = device.address
            self.latest[address] = reading
            self._answered[address] = now
            if address in self._lost:
                self._lost.discard(address)
                self._say(t("climate.back", who=self._who(device), reading=reading_text(reading)), silent=True)
            self._check_battery(device, reading, now)
            self._check_tips(device, reading, now)
            limits = device.limits_from(self.cfg.limits)
            if limits.alarm:
                self._check_cold(device, reading.temperature, limits.cold_below, now)
        self._flush()

    def no_answer(self, device: Thermometer) -> None:
        with self._lock:
            now = self.clock()
            address = device.address
            since = self._answered.get(address, self._started)
            if address in self._lost or now - since < timedelta(minutes=self.cfg.lost_minutes):
                return
            self._lost.add(address)
            text = t("climate.lost", who=self._who(device), duration=fmt_duration((now - since).total_seconds()))
            last = self.latest.get(address)
            if last is not None:
                text += " " + t("climate.lost_last", time=f"{last.time:%H:%M}", reading=reading_text(last))
            self._say(text, silent=True)
        self._flush()

    def poll_minutes(self) -> float:
        """Read more often while it is cold or close to it, so the alarm follows the temperature."""
        cold = self.cfg.cold
        with self._lock:
            if self.cold:
                return min(cold.every_minutes, self.cfg.every_minutes)
            for address, reading in self.latest.items():
                device = self._devices.get(address)
                limits = device.limits_from(self.cfg.limits) if device else None
                if limits and limits.alarm and reading.temperature < limits.cold_below + cold.close_degrees:
                    return min(cold.every_minutes, self.cfg.every_minutes)
        return self.cfg.every_minutes

    # Called by the siren every second and by the bot.

    def tick(self) -> str | None:
        """Send the alarm messages that are due, and say what the computer should sound: "alarm",
        "quiet" or None."""
        sound = None
        with self._lock:
            now = self.clock()
            self._check_silent(now)
            repeat = timedelta(minutes=self.cfg.cold.repeat_minutes)
            for address, state in self.cold.items():
                if state.stage == "alarm":
                    sound = "alarm"
                    if state.sent is None or now - state.sent >= repeat:
                        first = state.sent is None
                        state.sent = now
                        self._alarm_message(address, state, first)
                elif state.stage == "quiet" and sound is None:
                    sound = "quiet"
        self._flush()
        return sound

    def press(self, data: str) -> Reply | None:
        """The Off button under an alarm message."""
        if data != OFF:
            return None
        with self._lock:
            now = self.clock()
            ringing = [address for address, state in self.cold.items() if state.stage == "alarm"]
            for address in ringing:
                reading = self.latest.get(address)
                temperature = reading.temperature if reading else self.cold[address].reference
                self.cold[address] = Cold("off", now, temperature)
            # Pressed on a second phone after the first: it is off all the same.
            silenced = any(state.stage == "off" for state in self.cold.values())
        minutes = f"{self.cfg.cold.snooze_minutes:g}"
        if ringing:
            log.info(t("climate.off_log", minutes=minutes))
        return Reply(t("climate.off", minutes=minutes) if ringing or silenced else t("climate.off_none"))

    def status_lines(self) -> list[str]:
        """For /now: an alarm going on, or turned off for now."""
        lines = []
        with self._lock:
            for address, state in self.cold.items():
                name = self._name(self._devices[address])
                if state.stage == "alarm":
                    lines.append(t("climate.now_alarm", name=name))
                elif state.stage == "quiet":
                    lines.append(t("climate.now_quiet", name=name))
                else:
                    back = state.since + timedelta(minutes=self.cfg.cold.snooze_minutes)
                    lines.append(t("climate.now_off", name=name, time=f"{back:%H:%M}"))
        return lines

    # Inside the lock.

    def _check_cold(self, device: Thermometer, temperature: float, limit: float, now: datetime) -> None:
        cfg: ColdAlarmConfig = self.cfg.cold
        address = device.address
        state = self.cold.get(address)
        name = self._name(device)
        if state is None:
            if temperature < limit:
                self.cold[address] = Cold("quiet", now, temperature)
                text = t("climate.cold", name=name, temperature=f"{temperature:.1f}", limit=f"{limit:g}",
                         minutes=f"{cfg.check_minutes:g}")
                log.warning(text)
                self._say(text, silent=False)
            return
        if temperature >= limit + cfg.warming_degrees:
            del self.cold[address]
            text = t("climate.warm" if state.stage == "quiet" else "climate.warm_alarm", name=name,
                     temperature=f"{temperature:.1f}")
            log.info(text)
            # Without a sound: whoever got up for the alarm is awake anyway, and whoever turned it
            # off and went back to sleep should not be woken by good news.
            self._say(text, silent=True)
            return
        warmer = temperature >= state.reference + cfg.warming_degrees
        colder = temperature <= state.reference - cfg.colder_degrees
        waited = now - state.since
        if state.stage == "quiet":
            if colder:
                self.cold[address] = Cold("alarm", now, temperature, "climate.alarm_colder")
            elif waited >= timedelta(minutes=cfg.check_minutes):
                # Getting warmer by itself (someone closed the window): give it another while.
                self.cold[address] = Cold("quiet", now, temperature) if warmer else Cold("alarm", now, temperature)
        elif state.stage == "off":
            if colder:
                self.cold[address] = Cold("alarm", now, temperature, "climate.alarm_colder")
            elif waited >= timedelta(minutes=cfg.snooze_minutes):
                self.cold[address] = (
                    Cold("quiet", now, temperature) if warmer else Cold("alarm", now, temperature, "climate.alarm_again")
                )

    def _check_silent(self, now: datetime) -> None:
        """A thermometer that stopped answering while it was cold (a coin cell gives up in the
        cold) cannot say it got warmer, so it counts as not warmer: the alarm. Its decision would
        come with the first reading after check_minutes or snooze_minutes; a poll later, it is
        overdue. A poll is the pause, the search and the reads, so a minute is added for those."""
        cfg = self.cfg.cold
        poll = timedelta(minutes=cfg.every_minutes, seconds=self.cfg.scan_seconds + 60)
        for address, state in list(self.cold.items()):
            wait = {"quiet": cfg.check_minutes, "off": cfg.snooze_minutes}.get(state.stage)
            if wait is None or now - state.since < timedelta(minutes=wait) + poll:
                continue
            reading = self.latest.get(address)
            if reading is None or reading.time < state.since + timedelta(minutes=wait):
                self.cold[address] = Cold("alarm", now, state.reference, "climate.alarm_silent")

    def _alarm_message(self, address: str, state: Cold, first: bool) -> None:
        reading = self.latest.get(address)
        temperature = reading.temperature if reading else state.reference
        text = t(state.reason, name=self._name(self._devices[address]), temperature=f"{temperature:.1f}",
                 minutes=f"{self.cfg.cold.snooze_minutes:g}")
        if first:
            log.warning(text)
            self._notes.append(text)
        self._say(Reply(text, [[(t("climate.btn_off"), OFF)]]), silent=False)

    def _check_battery(self, device: Thermometer, reading: Reading, now: datetime) -> None:
        if reading.battery >= self.cfg.low_battery_volts:
            return
        told = self._battery_told.get(device.address)
        if told is not None and now - told < timedelta(days=1):
            return
        self._battery_told[device.address] = now
        self._say(t("climate.battery", who=self._who(device), volts=f"{reading.battery:.2f}",
                    percent=battery_percent(reading.battery)), silent=True)

    def _tips(self, device: Thermometer, reading: Reading) -> list[tuple[str, str]]:
        limits = device.limits_from(self.cfg.limits)
        # Where the cold starts the alarm, the alarm speaks for it.
        return [(kind, text) for kind, text in tips(reading, limits) if not (kind == "cold" and limits.alarm)]

    def _check_tips(self, device: Thermometer, reading: Reading, now: datetime) -> None:
        """A tip goes out when its trouble begins, without a sound, and no sooner than
        tips_every_hours after the same one: around a limit it would come and go every poll."""
        found = self._tips(device, reading)
        before = self._tips_now.get(device.address, set())
        self._tips_now[device.address] = {kind for kind, _ in found}
        for kind, tip in found:
            told = self._tips_told.get((device.address, kind))
            if kind in before or (told is not None and now - told < timedelta(hours=self.cfg.tips_every_hours)):
                continue
            self._tips_told[(device.address, kind)] = now
            self._say(t("climate.tip_message", name=self._name(device), reading=reading_text(reading), tip=tip),
                      silent=True)

    def _name(self, device: Thermometer) -> str:
        return label(device, len(self.cfg.devices))

    def _who(self, device: Thermometer) -> str:
        """The thermometer as the subject of a sentence: 'Thermometer "Crib"', or 'The thermometer'."""
        if not device.name and len(self.cfg.devices) == 1:
            return t("thermo.who_one")
        return t("thermo.who", name=self._name(device))

    def _say(self, text: str | Reply, silent: bool) -> None:
        self._outbox.append((text, silent))

    # Outside the lock.

    def _flush(self) -> None:
        with self._lock:
            outbox, self._outbox = self._outbox, []
            notes, self._notes = self._notes, []
        letters = [(self.notify, (text,)) for text in notes] + [(self.send, item) for item in outbox]
        for how, args in letters:
            if how is None:
                continue
            if self._mail is not None:
                self._mail.put((how, args))
            else:
                self._deliver(how, args)

    def _post(self) -> None:
        while (letter := self._mail.get()) is not None:
            self._deliver(*letter)

    @staticmethod
    def _deliver(how: Callable, args: tuple) -> None:
        try:
            how(*args)
        except Exception:  # one message lost is better than the rest of them
            log.exception(t("climate.failed"))


class Siren:
    """The cold alarm's sounds on this computer, in a thread of its own: a quiet beep once a
    minute at first, then a loud sound with no pause until the alarm is turned off."""

    def __init__(self, watch: ClimateWatch, cfg: ColdAlarmConfig, play: Callable[[str, float], None] | None = None):
        self.watch = watch
        self.cfg = cfg
        self.play = play or play_once
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="siren", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        last_quiet = None
        while not self._stop.wait(1):
            try:
                sound = self.watch.tick()
                if sound == "alarm":
                    self.play(self.cfg.alarm_sound, self.cfg.alarm_volume)
                elif sound == "quiet":
                    now = datetime.now()
                    if last_quiet is None or (now - last_quiet).total_seconds() >= QUIET_EVERY:
                        last_quiet = now
                        self.play(self.cfg.quiet_sound, self.cfg.quiet_volume)
                else:
                    last_quiet = None
            except Exception:  # keep sounding the next time rather than go quiet for good
                log.exception(t("climate.failed"))


def play_once(sound: str, volume: float) -> None:
    from .notify import sound_path

    try:
        subprocess.run(["afplay", "-v", f"{volume:g}", sound_path(sound)], check=False)
    except FileNotFoundError:  # afplay exists only on macOS
        pass
