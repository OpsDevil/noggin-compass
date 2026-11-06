"""The room climate: the cold alarm, tips about the heat and the humidity, and word in Telegram
when a thermometer is lost or its battery runs low.

The cold alarm is for a window or a door left open at night. It starts quietly, so that a
draught that passes by itself wakes nobody, and gets loud only if it does not get warmer. Its
sounds play on the computer running the program, and its messages go to Telegram with an Off
button; Off silences it for a while, and it comes back if it is still not warmer by then.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import ColdAlarmConfig, Thermometer, ThermometersConfig
from .i18n import t
from .notify import play
from .report import fmt_duration
from .telegram import Reply
from .thermometer import Reading, battery_percent, label, reading_text, stale, tips, who

log = logging.getLogger("noggin_compass")

OFF = "climate:off"  # data of the Off button
MUTE = "climate:mute:"  # + hours: data of the mute buttons
UNMUTE = "climate:unmute"
MUTE_CHOICES = (1, 2, 3, 4)  # hours offered as buttons
# See ClimateWatch._check_silent.
STUCK_POLLS = 3


@dataclass
class Cold:
    stage: str  # "quiet", "alarm", or "off" for an alarm turned off for a while
    since: datetime  # when the stage began
    reference: float  # the temperature then: "warmer" means above it by warming_degrees
    reason: str = "climate.alarm"  # what the alarm message says
    sent: datetime | None = None  # the last alarm message


class ClimateWatch:
    """Gets each reading from the ThermometerPoller, keeps the latest ones (the only copy: /now,
    the preview and the alarm all read them here) and decides what to say and what to sound.
    `send(text, silent)` posts to Telegram and `notify(text)` shows a notification on the computer;
    both only queue it (notify.Outbox). Called from the poller's, the bot's and the siren's
    threads, hence the lock."""

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
        self._polled: datetime | None = None  # start of the last poll that finished
        # Muted from Telegram until then (/mute): no cold alarm, no beeping. Only the cold alarm:
        # "time to turn" and the silent camera are not about the room's temperature.
        self._muted_until: datetime | None = None
        self._lock = threading.Lock()

    def readings(self) -> dict[str, Reading]:
        with self._lock:
            return dict(self.latest)

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
                if not stale(reading, now, self.cfg.every_minutes):
                    limits = device.limits_from(self.cfg.limits)
                    self._tips_now[address] = {kind for kind, _ in tips(reading, limits)}

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
            if limits.alarm and not self._muted(now):
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

    def polled(self, started: datetime) -> None:
        """A poll that began at `started` has finished: every thermometer had its chance."""
        with self._lock:
            self._polled = started

    def poll_minutes(self) -> float:
        """Read more often while it is cold or close to it, so the alarm follows the temperature."""
        cold = self.cfg.cold
        with self._lock:
            if self._muted(self.clock()):
                return self.cfg.every_minutes
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
            if self._muted_until is not None and now >= self._muted_until:
                self._muted_until = None
                log.info(t("climate.mute_over"))
                self._say(t("climate.mute_over"), silent=True)
            if self._muted(now):
                self.cold.clear()
                return None
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

    def mute(self, hours: float) -> Reply:
        """Mute the cold alarm for `hours`: what is going on stops at once, and if it is still cold
        when the time is up, it starts over from the quiet beep."""
        with self._lock:
            until = self.clock() + timedelta(hours=hours)
            self._muted_until = until
            self.cold.clear()
        log.info(t("climate.mute_log", time=f"{until:%H:%M}"))
        return self._mute_reply(until)

    def unmute(self) -> Reply:
        with self._lock:
            self._muted_until = None
        log.info(t("climate.unmute_log"))
        return Reply(t("climate.unmuted"), [[(t("climate.btn_mute", hours=self._hours()), MUTE + self._hours())]])

    def _mute_reply(self, until: datetime) -> Reply:
        hours = [(t("climate.btn_hours", hours=n), f"{MUTE}{n}") for n in MUTE_CHOICES]
        return Reply(t("climate.muted", time=f"{until:%H:%M}"), [hours, [(t("climate.btn_unmute"), UNMUTE)]])

    def _hours(self) -> str:
        return f"{self.cfg.cold.mute_hours:g}"

    def _mute_button(self) -> list[tuple[str, str]]:
        return [(t("climate.btn_mute", hours=self._hours()), MUTE + self._hours())]

    def press(self, data: str) -> Reply | None:
        """The buttons under the cold messages and under /mute's answer."""
        if data.startswith(MUTE):
            try:
                hours = float(data.removeprefix(MUTE))
            except ValueError:
                return None
            return self.mute(hours)
        if data == UNMUTE:
            return self.unmute()
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
        """For /now: an alarm going on, or turned off for now, or muted."""
        lines = []
        with self._lock:
            if self._muted(self.clock()):
                return [t("climate.now_muted", time=f"{self._muted_until:%H:%M}")]
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

    def _muted(self, now: datetime) -> bool:
        return self._muted_until is not None and now < self._muted_until

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
                # Opened the window on purpose? One tap mutes it.
                self._say(Reply(text, [self._mute_button()]), silent=False)
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
        cold) cannot say it got warmer, so it counts as not warmer: the alarm. Its decision is due
        check_minutes or snooze_minutes into the stage; once a whole poll has begun after that and
        finished without a reading from it, it has stopped answering. If no poll finishes at all
        (Bluetooth hangs), the same after STUCK_POLLS polls' worth of time."""
        cold = self.cfg.cold
        poll = timedelta(minutes=min(cold.every_minutes, self.cfg.every_minutes), seconds=self.cfg.scan_seconds)
        for address, state in list(self.cold.items()):
            wait = {"quiet": cold.check_minutes, "off": cold.snooze_minutes}.get(state.stage)
            if wait is None:
                continue
            due = state.since + timedelta(minutes=wait)
            reading = self.latest.get(address)
            if now < due or (reading is not None and reading.time >= due):
                continue  # not yet, or the reading that decides it has come
            missed = self._polled is not None and self._polled >= due and (reading is None or reading.time < self._polled)
            if missed or now - due >= STUCK_POLLS * poll:
                self.cold[address] = Cold("alarm", now, state.reference, "climate.alarm_silent")

    def _alarm_message(self, address: str, state: Cold, first: bool) -> None:
        reading = self.latest.get(address)
        temperature = reading.temperature if reading else state.reference
        text = t(state.reason, name=self._name(self._devices[address]), temperature=f"{temperature:.1f}",
                 minutes=f"{self.cfg.cold.snooze_minutes:g}")
        if first:
            log.warning(text)
            self._notes.append(text)
        self._say(Reply(text, [[(t("climate.btn_off"), OFF)], self._mute_button()]), silent=False)

    def _check_battery(self, device: Thermometer, reading: Reading, now: datetime) -> None:
        if reading.battery >= self.cfg.low_battery_volts:
            return
        told = self._battery_told.get(device.address)
        if told is not None and now - told < timedelta(days=1):
            return
        self._battery_told[device.address] = now
        self._say(t("climate.battery", who=self._who(device), volts=f"{reading.battery:.2f}",
                    percent=battery_percent(reading.battery)), silent=True)

    def _check_tips(self, device: Thermometer, reading: Reading, now: datetime) -> None:
        """A tip goes out when its trouble begins, without a sound, and no sooner than
        tips_every_hours after the same one: around a limit it would come and go every poll."""
        found = tips(reading, device.limits_from(self.cfg.limits))
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
        return who(device, len(self.cfg.devices))

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
            try:
                how(*args)
            except Exception:  # one message lost is better than the rest of them
                log.exception(t("climate.failed"))


class Siren:
    """The cold alarm's sounds on this computer, in a thread of its own: a beep every
    quiet_every_seconds at first, then the siren with no pause until the alarm is turned off."""

    def __init__(self, watch: ClimateWatch, cfg: ColdAlarmConfig, sound: Callable[[str, float], None] | None = None):
        self.watch = watch
        self.cfg = cfg
        self.sound = sound or play
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="siren", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        last_quiet = None
        while not self._stop.is_set():
            try:
                sound = self.watch.tick()
                if sound == "alarm":
                    began = time.monotonic()
                    self.sound(self.cfg.alarm_sound, self.cfg.alarm_volume)
                    # Back to back: a real alarm does not pause. One that could not play at all
                    # (no afplay) would spin, so then wait as usual.
                    if time.monotonic() - began > 0.2:
                        continue
                elif sound == "quiet":
                    now = datetime.now()
                    if last_quiet is None or (now - last_quiet).total_seconds() >= self.cfg.quiet_every_seconds:
                        last_quiet = now
                        self.sound(self.cfg.quiet_sound, self.cfg.quiet_volume)
                else:
                    last_quiet = None
            except Exception:  # keep sounding the next time rather than go quiet for good
                log.exception(t("climate.failed"))
            self._stop.wait(0.5)
