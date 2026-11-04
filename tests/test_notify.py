import io
import json
import urllib.parse
from datetime import datetime, timedelta

import numpy as np
import pytest

from noggin_compass import keychain, notify, telegram
from noggin_compass.balance import Alert
from noggin_compass.config import Config
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.i18n import set_language
from noggin_compass.storage import Storage
from noggin_compass.telegram import Photo
from noggin_compass.tracker import Segment

T0 = datetime(2026, 11, 4, 22, 0)
H = 3600.0


class FakeTelegram:
    """Stands in for urlopen: records the calls and returns the given updates from getUpdates."""

    def __init__(self, updates=()):
        self.calls = []
        self.updates = list(updates)

    def __call__(self, request, timeout):
        method = request.full_url.rsplit("/", 1)[1]
        if request.get_header("Content-type", "").startswith("multipart"):
            params = {"multipart": request.data}
        else:
            params = dict(urllib.parse.parse_qsl(request.data.decode()))
        self.calls.append((method, params))
        result = self.updates if method == "getUpdates" else {"message_id": 1}
        return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())

    def sent(self):
        return [(int(p["chat_id"]), p["text"]) for m, p in self.calls if m == "sendMessage"]

    def photos(self):
        return [p["multipart"] for m, p in self.calls if m == "sendPhoto"]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(notify, "play_sound", lambda sound: None)
    monkeypatch.setattr(notify, "mac_notification", lambda text: None)
    monkeypatch.delenv(telegram.TOKEN_ENV, raising=False)
    monkeypatch.delenv(telegram.CHAT_IDS_ENV, raising=False)
    monkeypatch.setattr(keychain, "read", lambda service: None)


def stint_alert():
    return Alert("stint", Side.RIGHT, Side.LEFT, 3 * H + 300, {Side.LEFT: 0.5 * H, Side.RIGHT: 3.1 * H})


def test_alert_texts_in_both_languages():
    assert notify.format_alert(stint_alert(), 12) == (
        "Time to turn the baby: the head has been turned right for 3 h 05 min. Turn it left."
    )
    imbalance = Alert("imbalance", Side.RIGHT, Side.LEFT, 1.5 * H, {Side.LEFT: 0.5 * H, Side.RIGHT: 3.5 * H})
    set_language("ru")
    assert notify.format_alert(imbalance, 12) == (
        "Пора повернуть малыша: за последние 12 ч голова была повёрнута вправо 3 ч 30 мин, "
        "а влево всего 30 мин. Поверните её влево."
    )


def test_token_from_keychain_as_last_resort(monkeypatch):
    monkeypatch.setattr(keychain, "read", lambda service: "kc-token" if service == telegram.KEYCHAIN_SERVICE else None)
    assert telegram.resolve_credentials(None, [1]) == ("kc-token", [1])
    assert telegram.resolve_credentials("cfg-token", [1]) == ("cfg-token", [1])


def test_credentials_from_environment_win(monkeypatch):
    assert telegram.resolve_credentials("cfg-token", [1]) == ("cfg-token", [1])
    monkeypatch.setenv(telegram.TOKEN_ENV, "env-token")
    monkeypatch.setenv(telegram.CHAT_IDS_ENV, "10, 20")
    assert telegram.resolve_credentials("cfg-token", [1]) == ("env-token", [10, 20])


def test_bot_answers_only_allowed_chats(caplog):
    fake = FakeTelegram()
    bot = telegram.TelegramBot("token", [10], handler=lambda text: f"echo {text}", opener=fake)
    updates = [
        {"update_id": 5, "message": {"chat": {"id": 10}, "text": "/now"}},
        {"update_id": 6, "message": {"chat": {"id": 99}, "text": "/now"}},
    ]
    with caplog.at_level("INFO"):
        assert bot.handle_updates(updates) == 7
    assert fake.sent() == [(10, "echo /now")]
    assert "99" in caplog.text


def test_alert_goes_to_telegram(monkeypatch):
    cfg = Config()
    cfg.telegram.token, cfg.telegram.chat_ids = "token", [10, 20]
    notifier = notify.Notifier(cfg)
    fake = FakeTelegram()
    notifier.bot._open = fake
    notifier.alert(stint_alert())
    assert [chat for chat, _ in fake.sent()] == [10, 20]
    assert "Turn it left" in fake.sent()[0][1]


def test_now_and_stats_commands(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    cfg.alerts.max_same_side_minutes = 180
    storage = Storage(cfg.storage.db_path)
    for i in range(1200):
        storage.add(T0 + timedelta(seconds=i), HeadEstimate(Side.RIGHT, -0.8))
    storage.close()

    notifier = notify.Notifier(cfg)
    assert "No data" in notifier.handle_command("/now")
    monitor = notify.monitor_from_config(cfg)
    current = Segment(Side.RIGHT, T0, T0 + timedelta(hours=3, minutes=5))
    notifier.update(current.end, current, monitor)

    now = notifier.handle_command("/now")
    assert "turned right, on this side for 3 h 05 min" in now
    assert "Time to turn it left." in now
    stats = notifier.handle_command("/stats@noggin_bot")
    assert "right 20 min (100%)" in stats
    assert "/now" in notifier.handle_command("/start")


def test_history_from_the_database_survives_a_restart(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    cfg.alerts.max_same_side_minutes = 180
    storage = Storage(cfg.storage.db_path)
    for i in range(0, 2 * 3600, 5):  # 2 hours to the right, then a restart
        storage.add(T0 + timedelta(seconds=i), HeadEstimate(Side.RIGHT, -0.8))
    restart = T0 + timedelta(hours=2, minutes=1)

    monitor = notify.monitor_from_config(cfg)
    monitor.add(notify.load_history(storage, cfg, until=restart))
    storage.close()
    current = Segment(Side.RIGHT, restart, restart + timedelta(minutes=30))
    totals = monitor.totals(current.end, current)
    assert round(totals[Side.RIGHT] / 60) == 150
    assert round(monitor.stint(current.end, current) / 60) == 150


def test_sound_by_name_or_file(tmp_path):
    assert notify.sound_path("Funk") == "/System/Library/Sounds/Funk.aiff"
    own = tmp_path / "beep.wav"
    own.write_bytes(b"RIFF")
    assert notify.sound_path(str(own)) == str(own)


def test_photo_commands(tmp_path):
    cfg = Config()
    notifier = notify.Notifier(cfg)
    assert "telegram.photos" in notifier.handle_command("/photo")

    cfg.telegram.photos = True
    assert "No data" in notifier.handle_command("/photo")
    image = np.full((200, 300, 3), 120, dtype=np.uint8)
    notifier.set_frame(image, None, HeadEstimate(Side.LEFT, 0.62), T0)
    for command in ("/photo", "/marked"):
        photo = notifier.handle_command(command)
        assert isinstance(photo, Photo)
        assert photo.jpeg[:2] == b"\xff\xd8"
        assert photo.caption == "22:00:00, head turned left (score +0.62)"


def test_bot_sends_photo_as_multipart():
    fake = FakeTelegram()
    bot = telegram.TelegramBot("token", [10], handler=lambda text: Photo(b"\xff\xd8jpeg", "caption"), opener=fake)
    bot.handle_updates([{"update_id": 1, "message": {"chat": {"id": 10}, "text": "/photo"}}])
    (body,) = fake.photos()
    assert b'name="chat_id"' in body and b"10" in body
    assert b'name="photo"; filename="frame.jpg"' in body and b"\xff\xd8jpeg" in body


def test_command_menu_lists_photos_only_when_allowed():
    cfg = Config()
    cfg.telegram.token, cfg.telegram.chat_ids = "token", [10]
    assert [name for name, _ in notify.Notifier(cfg).bot.commands] == ["now", "stats"]
    cfg.telegram.photos = True
    assert [name for name, _ in notify.Notifier(cfg).bot.commands] == ["now", "stats", "photo", "marked"]
