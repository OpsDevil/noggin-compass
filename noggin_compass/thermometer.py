"""Room thermometers over Bluetooth: the Xiaomi Mijia LYWSD03MMC on its stock firmware.

The thermometer broadcasts its readings encrypted with a key from the Xiaomi account, but to a
client that connects it sends them in the open. So every every_minutes the program connects to
each chosen thermometer for a few seconds, takes one reading and disconnects: staying connected
would drain the coin cell, and the phone app could not connect meanwhile.

The thermometers are chosen with `noggin-compass thermometer`, which lists the ones nearby with
their readings. On a Mac a Bluetooth device has no fixed MAC address: each Mac gives it its own
identifier, so the address comes from that list rather than from the sticker.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime

from .config import ClimateLimits, Thermometer
from .i18n import t
from .presence import describe
from .report import fmt_duration

log = logging.getLogger("noggin_compass")

# A characteristic that sends temperature, humidity and battery voltage to a connected client
# every ~6 seconds: int16 LE hundredths of a degree, uint8 percent, uint16 LE millivolts.
READINGS = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"
MODELS = ("LYWSD03MMC",)
CONNECT_TIMEOUT = 40.0  # a weak signal (-80 dBm and below) can take half a minute to connect
READ_TIMEOUT = 20.0
GOOD_SIGNAL = -70  # dBm
WEAK_SIGNAL = -85


@dataclass(frozen=True)
class Reading:
    time: datetime
    temperature: float  # degrees Celsius
    humidity: int  # percent
    battery: float  # volts


@dataclass(frozen=True)
class Found:
    """A thermometer seen in a scan. `device` is bleak's handle, to connect without another scan."""

    address: str
    model: str
    rssi: int
    device: object = None


def parse(data: bytes, time: datetime) -> Reading:
    temperature, humidity, millivolts = struct.unpack("<hBH", bytes(data[:5]))
    return Reading(time, temperature / 100, humidity, millivolts / 1000)


async def scan(seconds: float) -> list[Found]:
    """Supported thermometers nearby, strongest signal first."""
    from bleak import BleakScanner

    seen = await BleakScanner.discover(timeout=seconds, return_adv=True)
    found = [
        Found(device.address, adv.local_name or device.name, adv.rssi, device)
        for device, adv in seen.values()
        if (adv.local_name or device.name) in MODELS
    ]
    return sorted(found, key=lambda f: -f.rssi)


async def read(target: str | object, scan_seconds: float) -> Reading:
    """One reading from a thermometer given by its address or by a device from scan()."""
    from bleak import BleakClient, BleakScanner

    device = target
    if isinstance(target, str):
        # It advertises only every few seconds, and with a weak signal many are lost, so look
        # for it longer than bleak's default 10 s. The search ends as soon as it is seen.
        device = await BleakScanner.find_device_by_address(target, timeout=scan_seconds)
        if device is None:
            raise LookupError(t("thermo.not_found", seconds=f"{scan_seconds:g}"))
    loop = asyncio.get_running_loop()
    first: asyncio.Future[bytes] = loop.create_future()

    def on_data(_, data: bytearray) -> None:
        # Called from bleak's own thread on some systems: hand the data over to the loop.
        loop.call_soon_threadsafe(lambda: first.done() or first.set_result(bytes(data)))

    async with BleakClient(device, timeout=CONNECT_TIMEOUT) as client:
        await client.start_notify(READINGS, on_data)
        data = await asyncio.wait_for(first, READ_TIMEOUT)
    return parse(data, datetime.now())


async def read_all(addresses: list[str], scan_seconds: float):
    """Readings from several thermometers with one search. Each is read as soon as it is seen, so
    one with a weak signal does not hold up the others. Yields (address, reading or the error)."""
    from bleak import BleakScanner

    loop = asyncio.get_running_loop()
    missing = {address.upper(): address for address in addresses}
    seen: asyncio.Queue = asyncio.Queue()

    def detected(device, _advertisement) -> None:
        address = missing.pop(device.address.upper(), None)
        if address is not None:
            loop.call_soon_threadsafe(seen.put_nowait, (address, device))

    deadline = loop.time() + scan_seconds
    unread = set(addresses)
    async with BleakScanner(detection_callback=detected):
        while unread:
            # One seen already is read even when the time is up.
            try:
                address, device = seen.get_nowait()
            except asyncio.QueueEmpty:
                if deadline <= loop.time():
                    break
                try:
                    address, device = await asyncio.wait_for(seen.get(), deadline - loop.time())
                except asyncio.TimeoutError:
                    break
            unread.discard(address)
            try:
                yield address, await read(device, scan_seconds)
            except Exception as error:
                yield address, error
    for address in addresses:
        if address in unread:
            yield address, LookupError(t("thermo.not_found", seconds=f"{scan_seconds:g}"))


def battery_percent(volts: float) -> int:
    """Roughly how much is left in the CR2032, as custom firmwares for this thermometer count it:
    3.1 V is full, 2.2 V is empty, a straight line in between. A coin cell holds about 2.9 V most
    of its life and then drops fast, so it is a hint rather than a gauge."""
    return max(0, min(100, round((volts - 2.2) / 0.009)))


def label(device: Thermometer, count: int) -> str:
    """A thermometer's name wherever one is shown: its own, "Thermometer" when it is the only one,
    or the end of its address. Lines, messages and the log all name it through here."""
    if device.name:
        return device.name
    return t("thermo.label") if count == 1 else device.address[-4:]


def who(device: Thermometer, count: int) -> str:
    """The thermometer as the subject of a sentence: 'Thermometer "Crib"', or 'The thermometer'."""
    if not device.name and count == 1:
        return t("thermo.who_one")
    return t("thermo.who", name=label(device, count))


# A reading older than this many polls is no longer "now": it is shown with its age, and no tip
# is given from it, as what it showed may be over.
STALE_POLLS = 2


def stale(reading: Reading, now: datetime, every_minutes: float) -> bool:
    return (now - reading.time).total_seconds() > STALE_POLLS * every_minutes * 60


def tips(reading: Reading, limits: ClimateLimits) -> list[tuple[str, str]]:
    """What in this reading is off for a baby's sleep, as (hot/cold/dry/humid, advice), the same
    for /now and for Telegram. Cold is a tip only where it does not start the alarm: where it
    does, the alarm speaks for it."""
    found = []
    degrees = {"low": f"{limits.cold_below:g}", "high": f"{limits.hot_above:g}"}
    if reading.temperature > limits.hot_above:
        found.append(("hot", t("climate.tip.hot", **degrees)))
    elif reading.temperature < limits.cold_below and not limits.alarm:
        found.append(("cold", t("climate.tip.cold", **degrees)))
    percent = {"low": f"{limits.dry_below:g}", "high": f"{limits.humid_above:g}"}
    if reading.humidity < limits.dry_below:
        found.append(("dry", t("climate.tip.dry", **percent)))
    elif reading.humidity > limits.humid_above:
        found.append(("humid", t("climate.tip.humid", **percent)))
    return found


def signal_label(rssi: int) -> str:
    if rssi >= GOOD_SIGNAL:
        return t("thermo.signal_good")
    return t("thermo.signal_fair") if rssi >= WEAK_SIGNAL else t("thermo.signal_weak")


def reading_text(reading: Reading) -> str:
    return t("thermo.reading", temperature=f"{reading.temperature:.1f}", humidity=reading.humidity)


def climate_lines(
    devices: list[Thermometer],
    readings: dict[str, Reading],
    now: datetime,
    every_minutes: float,
    low_battery_volts: float = 0.0,
    battery: bool = False,
) -> list[str]:
    """One line per thermometer for /now and the preview. The name only matters when there are
    several. A reading older than two polls gets its age, so an old number is not taken for now."""
    lines = []
    for device in devices:
        name = label(device, len(devices))
        reading = readings.get(device.address)
        if reading is None:
            text = t("thermo.no_reading_one") if len(devices) == 1 else t("thermo.no_reading", name=name)
            lines.append(text)
            continue
        text = reading_text(reading)
        if battery:
            text = t("thermo.with_battery", reading=text, percent=battery_percent(reading.battery))
        if stale(reading, now, every_minutes):
            text = t("thermo.ago", reading=text, ago=fmt_duration((now - reading.time).total_seconds()))
        lines.append(t("thermo.one", reading=text) if len(devices) == 1 else t("thermo.named", name=name, reading=text))
        if reading.battery < low_battery_volts:
            lines.append(t("thermo.battery_low_one") if len(devices) == 1 else t("thermo.battery_low", name=name))
    return lines


def tip_lines(
    devices: list[Thermometer], readings: dict[str, Reading], defaults: ClimateLimits, now: datetime, every_minutes: float
) -> list[str]:
    """The advice for /now, from readings no older than two polls: an old one may be over."""
    lines = []
    for device in devices:
        reading = readings.get(device.address)
        if reading is None or stale(reading, now, every_minutes):
            continue
        for _, tip in tips(reading, device.limits_from(defaults)):
            if len(devices) == 1:
                lines.append(t("climate.tip_line", tip=tip[0].upper() + tip[1:]))
            else:
                lines.append(t("climate.tip_line_named", name=label(device, len(devices)), tip=tip))
    return lines


class ThermometerPoller:
    """Reads the chosen thermometers every every_minutes in a thread of its own and saves the
    readings. Each result goes to `watch` (a ClimateWatch), which keeps the latest for /now, the
    preview and the alarm, and may ask to read more often while it is cold. A thermometer that
    does not answer is logged once and tried again at the next poll; the camera goes on either way."""

    def __init__(self, devices: list[Thermometer], db_path: str, every_minutes: float, scan_seconds: float,
                 watch=None):
        self.devices = devices
        self.db_path = db_path
        self.every_minutes = every_minutes
        self.scan_seconds = scan_seconds
        self.watch = watch
        self._failing: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="thermometers", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # A reading in progress can take a minute; the thread is a daemon, so do not wait for it.
        self._thread.join(timeout=2)

    def _run(self) -> None:
        from .storage import Storage  # its own connection: SQLite connections stay in one thread

        storage = Storage(self.db_path)
        try:
            asyncio.run(self._poll(storage))
        except Exception:  # never let the thread die silently
            log.exception(t("thermo.stopped"))
        finally:
            storage.close()

    async def _poll(self, storage) -> None:
        while not self._stop.is_set():
            started = datetime.now()
            await self._read_all(storage)
            self._tell("polled", started)
            minutes = self.every_minutes if self.watch is None else self.watch.poll_minutes()
            # Wait in short steps so that stop() is noticed.
            for _ in range(int(minutes * 60)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(1)

    async def _read_all(self, storage) -> None:
        devices = {device.address: device for device in self.devices}
        done = set()
        try:
            async with aclosing(read_all(list(devices), self.scan_seconds)) as results:
                async for address, result in results:
                    done.add(address)
                    self._result(devices[address], result, storage)
                    if self._stop.is_set():
                        return
        except Exception as error:  # Bluetooth turned off and the like: none of the rest was read
            for address, device in devices.items():
                if address not in done:
                    self._result(device, error, storage)

    def _result(self, device: Thermometer, result: Reading | Exception, storage) -> None:
        name = who(device, len(self.devices))
        if isinstance(result, Exception):
            if device.address not in self._failing:
                log.warning(t("thermo.failed", who=name, error=describe(result), minutes=f"{self.every_minutes:g}"))
            self._failing.add(device.address)
            self._tell("no_answer", device)
            return
        if device.address in self._failing:
            log.info(t("thermo.recovered", who=name))
        self._failing.discard(device.address)
        log.info(t("thermo.logged", who=name, reading=reading_text(result), battery=f"{result.battery:.2f}"))
        # The alarm hears of it even if it cannot be saved: a full disk must not silence it.
        self._tell("reading", device, result)
        try:
            storage.add_climate(device.address, result)
        except Exception:
            log.exception(t("thermo.not_saved", who=name))

    def _tell(self, what: str, *args) -> None:
        if self.watch is None:
            return
        try:
            getattr(self.watch, what)(*args)
        except Exception:  # the alarm has a bug: the readings still go on
            log.exception(t("climate.failed"))


async def survey(seconds: float) -> list[tuple[Found, Reading | None, str | None]]:
    """Thermometers nearby with a reading from each, or why there is none."""
    result = []
    for found in await scan(seconds):
        try:
            result.append((found, await read(found.device, seconds), None))
        except Exception as error:  # busy with another connection, out of range by now
            result.append((found, None, describe(error)))
    return result


def menu(cfg, config_path, seconds: float, ask=input, out=print, look=None) -> list[Thermometer] | None:
    """The terminal menu of `noggin-compass thermometer`: find the thermometers nearby, show
    their readings and let the user pick. Returns the new choice, or None if nothing changed.
    `look` is survey(), replaceable in tests."""
    from .config import save_thermometers

    chosen = {device.address: device for device in cfg.thermometers.devices}
    out(t("thermo.menu.scanning", seconds=f"{seconds:g}"))
    try:
        nearby = asyncio.run((look or survey)(seconds))
    except Exception as error:
        out(t("thermo.menu.scan_failed", error=describe(error)))
        return None
    # The chosen ones that are not around now stay in the list, so they can be kept or dropped.
    seen = {found.address for found, _, _ in nearby}
    rows = [(found.address, found, reading, error) for found, reading, error in nearby]
    rows += [(address, None, None, None) for address in chosen if address not in seen]
    if not rows:
        out(t("thermo.menu.none", models=", ".join(MODELS)))
        return None

    out(t("thermo.menu.found", count=len(nearby)))
    for n, (address, found, reading, error) in enumerate(rows, 1):
        if found is None:
            text = t("thermo.menu.missing")
        elif reading is None:
            text = t("thermo.menu.unread", error=error)
        else:
            text = t("thermo.menu.reading", reading=reading_text(reading), signal=signal_label(found.rssi),
                     battery=f"{reading.battery:.2f}")
        mark = ""
        if address in chosen:
            name = chosen[address].name
            mark = t("thermo.menu.chosen_as", name=name) if name else t("thermo.menu.chosen")
        out(t("thermo.menu.item", n=n, text=text, id=address[-4:], mark=mark))
    if len(rows) > 1:
        out(t("thermo.menu.hint"))

    while True:
        try:
            answer = ask(t("thermo.menu.ask")).replace(",", " ").split()
        except (EOFError, KeyboardInterrupt):
            out("")
            return None
        if not answer:
            _keep_readings(cfg, cfg.thermometers.devices, nearby)
            out(t("thermo.menu.unchanged"))
            return None
        if answer == ["0"]:
            numbers = []
            break
        try:
            numbers = list(dict.fromkeys(int(a) for a in answer))
        except ValueError:
            numbers = [0]
        wrong = [n for n in numbers if not 1 <= n <= len(rows)]
        if not wrong:
            break
        out(t("thermo.menu.bad", choice=" ".join(answer)))

    devices = []
    for n in numbers:
        address = rows[n - 1][0]
        name = chosen[address].name if address in chosen else ""
        if len(numbers) > 1:  # with one thermometer its name is never shown
            default = name or t("thermo.menu.default_name", n=n)
            try:
                name = ask(t("thermo.menu.ask_name", n=n, default=default)).strip() or default
            except (EOFError, KeyboardInterrupt):
                out("")
                return None
        devices.append(Thermometer(address, name, chosen[address].limits if address in chosen else {}))
    _keep_readings(cfg, devices, nearby)
    if devices == cfg.thermometers.devices:
        out(t("thermo.menu.unchanged"))
        return None
    save_thermometers(config_path, devices)
    out(t("thermo.menu.saved", path=config_path) if devices else t("thermo.menu.cleared", path=config_path))
    return devices


def _keep_readings(cfg, devices: list[Thermometer], nearby) -> None:
    """Save the readings the menu has just taken of the chosen thermometers, so that /now and the
    preview have them before the first poll."""
    chosen = {device.address for device in devices}
    readings = [(found.address, reading) for found, reading, _ in nearby if reading and found.address in chosen]
    if not readings:
        return
    from .storage import Storage

    storage = Storage(cfg.storage.db_path)
    try:
        for address, reading in readings:
            storage.add_climate(address, reading)
    finally:
        storage.close()
