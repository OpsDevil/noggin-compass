import io
import json
import urllib.parse
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest

from noggin_compass import keychain, notify, telegram
from noggin_compass.balance import Alert
from noggin_compass.config import Config
from noggin_compass.head import HeadEstimate, Side
from noggin_compass.i18n import set_language
from noggin_compass.preview import Frame, picture
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
    monkeypatch.setattr(notify, "play", lambda sound, volume=1.0, times=1: None)
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
    notifier.clock = lambda: current.end

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
    notes = ["Asleep for 40 min, since 21:20.", "Temperature 21.0 °C, humidity 50%."]
    frame = Frame(image, None, HeadEstimate(Side.LEFT, 0.62), T0, notes)
    notifier.set_frame(frame)
    for command in ("/photo", "/marked"):
        photo = notifier.handle_command(command)
        assert isinstance(photo, Photo)
        assert photo.image[:2] == b"\xff\xd8"
        assert photo.caption == "22:00:00, head turned left (score +0.62)"


def test_marked_is_the_preview_picture_with_its_notes():
    cfg = Config()
    cfg.telegram.photos = True
    image = np.full((600, 900, 3), 120, dtype=np.uint8)
    frame = Frame(image, None, HeadEstimate(Side.LEFT, 0.62), T0, ["Asleep for 40 min, since 21:20."])
    notifier = notify.Notifier(cfg)
    notifier.set_frame(frame)
    sent = cv2.imdecode(np.frombuffer(notifier.handle_command("/marked").image, np.uint8), cv2.IMREAD_COLOR)
    preview = picture(frame, cfg)
    assert sent.shape == preview.shape
    assert np.abs(sent.astype(int) - preview.astype(int)).mean() < 3  # the same picture, but for JPEG
    bare = picture(Frame(image, None, HeadEstimate(Side.LEFT, 0.62), T0), cfg)
    assert (bare[-80:, :400] != preview[-80:, :400]).any()  # the note is in the bottom left corner


def test_bot_sends_photo_as_multipart():
    fake = FakeTelegram()
    bot = telegram.TelegramBot("token", [10], handler=lambda text: Photo(b"\xff\xd8jpeg", "caption"), opener=fake)
    bot.handle_updates([{"update_id": 1, "message": {"chat": {"id": 10}, "text": "/photo"}}])
    (body,) = fake.photos()
    assert b'name="chat_id"' in body and b"10" in body
    assert b'name="photo"; filename="frame.jpg"' in body and b"\xff\xd8jpeg" in body
    assert b"Content-Type: image/jpeg" in body


def test_bot_sends_png_chart_as_png():
    fake = FakeTelegram()
    chart = Photo(b"\x89PNG", "caption", filename="chart.png")
    bot = telegram.TelegramBot("token", [10], handler=lambda text: chart, opener=fake)
    bot.handle_updates([{"update_id": 1, "message": {"chat": {"id": 10}, "text": "/chart"}}])
    (body,) = fake.photos()
    assert b'filename="chart.png"' in body and b"Content-Type: image/png" in body


def test_chart_command(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    notifier = notify.Notifier(cfg)
    notifier.status = notify.Status(time=T0 + timedelta(hours=1))
    notifier.clock = lambda: T0 + timedelta(hours=1)
    empty = notifier.handle_command("/chart")
    assert empty.text == "No data yet." and empty.buttons  # the other days can still be picked
    storage = Storage(cfg.storage.db_path)
    for i in range(0, 3600, 5):
        side, score = (Side.LEFT, 0.8) if i < 1800 else (Side.RIGHT, -0.8)
        storage.add(T0 + timedelta(seconds=i), HeadEstimate(side, score), 0.0)
    storage.close()
    chart = notifier.handle_command("/chart")
    assert isinstance(chart, Photo) and chart.image[:4] == b"\x89PNG"
    assert chart.filename == "chart.png"
    # Buttons under it: the last 24 hours, today, yesterday and the days before.
    labels = [label for row in chart.buttons for label, _ in row]
    assert labels[:3] == ["Last 24 hours", "Today", "Yesterday"] and len(labels) == 8
    assert ("Today", "chart:2026-11-04") in chart.buttons[0]
    day = notifier.press("chart:2026-11-04")
    assert isinstance(day, Photo) and day.buttons == chart.buttons
    assert notifier.press("chart:2026-11-01").text == "No data for 01.11."
    assert notifier.press("chart:garbage") is None


def test_an_answer_to_a_button_under_a_picture_comes_as_a_new_message():
    sent, photos, edits = [], [], []
    bot = telegram.TelegramBot("token", [10], lambda text: None,
                               on_button=lambda data: Photo(b"\x89PNG", "day") if data == "pic" else "text")
    bot.call = lambda method, **params: edits.append(method)
    bot.send = lambda text, chat_ids=None, silent=False: sent.append(text)
    bot.send_photo = lambda chat, photo: photos.append(photo.caption) or True
    picture_message = {"chat": {"id": 10}, "message_id": 5, "photo": [{}]}
    bot.handle_updates([{"update_id": 1, "callback_query": {"id": "q", "data": "pic", "message": picture_message}},
                        {"update_id": 2, "callback_query": {"id": "r", "data": "text", "message": picture_message}}])
    assert photos == ["day"] and sent == ["text"] and "editMessageText" not in edits


def test_command_menu_lists_photos_only_when_allowed():
    cfg = Config()
    cfg.telegram.token, cfg.telegram.chat_ids = "token", [10]
    base = ["now", "stats", "sleep", "nap", "naps", "chart"]
    assert [name for name, _ in notify.Notifier(cfg).bot.commands] == base
    cfg.telegram.photos = True
    assert [name for name, _ in notify.Notifier(cfg).bot.commands] == base + ["photo", "marked"]


def test_bot_answers_about_the_last_24_hours(tmp_path):
    cfg = Config()
    cfg.storage.db_path = str(tmp_path / "db.sqlite")
    storage = Storage(cfg.storage.db_path)
    old = T0 - timedelta(hours=30)
    for i in range(0, 3600, 5):  # an hour on the right, more than 24 hours ago
        storage.add(old + timedelta(seconds=i), HeadEstimate(Side.RIGHT, -0.8), 0.0)
    for i in range(0, 1800, 5):  # half an hour on the left and asleep, just now
        storage.add(T0 + timedelta(seconds=i), HeadEstimate(Side.LEFT, 0.8), 0.0)
    storage.close()
    notifier = notify.Notifier(cfg)
    notifier.status = notify.Status(time=T0 + timedelta(minutes=30))
    notifier.clock = lambda: T0 + timedelta(minutes=30)
    stats = notifier.stats_text()
    assert stats.startswith("Last 24 hours:\nleft 30 min (100%)\nright 0 s")
    assert "sleep 30 min, sleeps: 1" in stats
    assert notifier.sleep_text().startswith("Sleep over the last 24 hours: 30 min, sleeps: 1")
    assert notifier.chart().caption == "Data as of 22:30"


def test_bot_sends_buttons_and_edits_the_message_on_a_press(caplog):
    fake = FakeTelegram()
    pressed = []

    def on_button(data):
        pressed.append(data)
        return telegram.Reply("done", [[("Again", "x:2")]])

    bot = telegram.TelegramBot(
        "token", [10], handler=lambda text: telegram.Reply("pick", [[("A", "x:1"), ("B", "x:2")]]),
        opener=fake, on_button=on_button,
    )
    bot.handle_updates([{"update_id": 1, "message": {"chat": {"id": 10}, "text": "/nap"}}])
    method, params = fake.calls[-1]
    assert method == "sendMessage" and params["text"] == "pick"
    assert json.loads(params["reply_markup"]) == {
        "inline_keyboard": [[{"text": "A", "callback_data": "x:1"}, {"text": "B", "callback_data": "x:2"}]]
    }

    press = {"id": "q1", "data": "x:1", "message": {"message_id": 7, "chat": {"id": 10}}}
    stranger = {"id": "q2", "data": "x:1", "message": {"message_id": 8, "chat": {"id": 99}}}
    with caplog.at_level("INFO"):
        bot.handle_updates([{"update_id": 2, "callback_query": press}, {"update_id": 3, "callback_query": stranger}])
    assert pressed == ["x:1"]
    methods = [m for m, _ in fake.calls[1:]]
    assert methods == ["answerCallbackQuery", "editMessageText", "answerCallbackQuery"]
    edit = fake.calls[2][1]
    assert (edit["chat_id"], edit["message_id"], edit["text"]) == ("10", "7", "done")
    assert "99" in caplog.text


def test_naps_count_in_sleep_and_stats(tmp_path):
    set_language("ru")
    try:
        cfg = Config()
        cfg.storage.db_path = str(tmp_path / "db.sqlite")
        storage = Storage(cfg.storage.db_path)
        for i in range(0, 1800, 5):  # half an hour asleep in the crib
            storage.add(T0 + timedelta(seconds=i), HeadEstimate(Side.LEFT, 0.8), 0.0)
        # A nap in arms in the afternoon, and one entered for the crib sleep the camera saw too.
        storage.add_nap(T0 - timedelta(hours=8), T0 - timedelta(hours=6, minutes=30))
        storage.add_nap(T0 + timedelta(minutes=10), T0 + timedelta(minutes=20))
        storage.close()
        notifier = notify.Notifier(cfg)
        notifier.status = notify.Status(time=T0 + timedelta(minutes=30))
        notifier.clock = lambda: T0 + timedelta(minutes=30)
        assert notifier.sleep_text().split("\n") == [
            "Сон за последние 24 часа: 2 ч 00 мин, засыпаний: 2, самый долгий: 1 ч 30 мин",
            "14:00-15:30  1 ч 30 мин (вне кроватки)",
            # Only the entered minutes are marked.
            "22:00-сейчас  30 мин (вне кроватки 22:10-22:20)",
        ]
        assert "сон 2 ч 00 мин, засыпаний: 2" in notifier.stats_text()
        assert isinstance(notifier.handle_command("/nap"), telegram.Reply)
        # /nap with a time logs it; /now tells a sleep outside the crib while one is open.
        assert notifier.handle_command("/nap 21:00-21:20").text.startswith("Записан сон вне кроватки: 21:00-21:20")
        notifier.naps.press("nap:sleep")
        notifier.status = notify.Status(T0 + timedelta(minutes=30), Segment(Side.ABSENT, T0, T0 + timedelta(minutes=30)),
                                        asleep="Сейчас не спит.")
        notifier.clock = lambda: T0 + timedelta(minutes=50)
        now = notifier.now_text()
        assert "Спит вне кроватки уже 20 мин, с 22:30." in now
        assert "Сейчас не спит." not in now
    finally:
        set_language("en")
