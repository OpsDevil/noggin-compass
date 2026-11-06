from datetime import datetime, timedelta

import pytest

from noggin_compass.i18n import set_language
from noggin_compass.naps import NapBook
from noggin_compass.storage import Storage

NOW = datetime(2026, 11, 5, 14, 45, 30)


@pytest.fixture()
def book(tmp_path):
    set_language("ru")
    clock = {"now": NOW}
    book = NapBook(str(tmp_path / "db.sqlite"), clock=lambda: clock["now"])
    book.clock_value = clock
    yield book
    set_language("en")


def stored(book):
    storage = Storage(book.db_path)
    try:
        return [(start, end) for _, start, end in storage.naps()]
    finally:
        storage.close()


def labels(reply):
    return [[label for label, _ in row] for row in reply.buttons]


def data(reply, label):
    return next(d for row in reply.buttons for text, d in row if text == label)


def test_menu_offers_durations_and_corrections(book):
    menu = book.menu()
    assert menu.text == "Сколько малыш спал вне кроватки? Сон закончился только что.\nДругое время: /nap 14:10-15:30"
    assert labels(menu) == [
        ["15 мин", "30 мин", "45 мин", "1 ч"],
        ["1 ч 30 мин", "2 ч", "2 ч 30 мин", "3 ч"],
        ["Уснул сейчас"],
        ["Изменить записанный сон"],
    ]


def test_a_press_logs_a_nap_that_ends_now(book):
    reply = book.press(data(book.menu(), "1 ч 30 мин"))
    assert reply.text == "Записан сон вне кроватки: 13:15-14:45, 1 ч 30 мин."
    assert labels(reply) == [["Изменить", "Удалить"]]
    assert stored(book) == [(datetime(2026, 11, 5, 13, 15), datetime(2026, 11, 5, 14, 45))]


def test_change_keeps_the_wake_up_time(book):
    added = book.press("nap:add:90")
    edit = book.press(data(added, "Изменить"))
    assert edit.text == (
        "Записано: 13:15-14:45, 1 ч 30 мин. Сколько малыш спал на самом деле?\n"
        "Другое время: удалите эту запись и пришлите /nap 14:10-15:30"
    )
    assert labels(edit)[-1] == ["Назад"]
    changed = book.press(data(edit, "45 мин"))
    assert changed.text == "Исправлено: 14:00-14:45, 45 мин."
    assert stored(book) == [(datetime(2026, 11, 5, 14, 0), datetime(2026, 11, 5, 14, 45))]


def test_list_shows_latest_first_and_leads_to_delete(book):
    book.clock_value["now"] = NOW - timedelta(days=1)
    book.press("nap:add:60")
    book.clock_value["now"] = NOW
    book.press("nap:add:30")
    listing = book.list()
    assert listing.text.startswith("Сон вне кроватки, сначала последний.")
    # Entries from another day carry the date.
    assert labels(listing) == [["14:15-14:45, 30 мин"], ["04.11 13:45-14:45, 1 ч 00 мин"]]
    entry = book.press(data(listing, "04.11 13:45-14:45, 1 ч 00 мин"))
    assert entry.text == "04.11 13:45-14:45, 1 ч 00 мин"
    assert labels(entry) == [["Изменить", "Удалить"], ["Назад"]]
    deleted = book.press(data(entry, "Удалить"))
    assert deleted.text == "Удалено: 04.11 13:45-14:45, 1 ч 00 мин."
    assert len(stored(book)) == 1
    # The other parent still has the old message with the deleted entry.
    assert book.press(data(entry, "Изменить")).text == "Этой записи уже нет."


def test_empty_list_and_foreign_buttons(book):
    assert book.list().text == "Сон вне кроватки ещё не записывали. Записать: /nap"
    assert book.list().buttons == []
    assert book.press("other:1") is None
    assert book.press("nap:add:abc") is None
    assert book.press("nap:show:42").text == "Этой записи уже нет."


def test_nap_entered_as_a_time(book):
    reply = book.command("13:05-14:20")
    assert reply.text == "Записан сон вне кроватки: 13:05-14:20, 1 ч 15 мин."
    assert labels(reply) == [["Изменить", "Удалить"]]
    # Till now, and a long dash a phone may put in.
    assert book.command("14:25").text == "Записан сон вне кроватки: 14:25-14:45, 20 мин."
    assert book.command("10.00 \u2014 10.30").text.startswith("Записан сон вне кроватки: 10:00-10:30, 30 мин.")
    assert book.command("").text.startswith("Сколько малыш спал")
    for wrong in ("abc", "25:00-26:00", "14:10-14:10", "14:60"):
        assert book.command(wrong).text.startswith("Пришлите время так: /nap 14:10-15:30")
    assert len(stored(book)) == 3


@pytest.mark.parametrize(
    "text, start, end",
    [
        # A time later than now is yesterday's.
        ("23:10-00:10", datetime(2026, 11, 4, 23, 10), datetime(2026, 11, 5, 0, 10)),
        ("15:00-16:00", datetime(2026, 11, 4, 15, 0), datetime(2026, 11, 4, 16, 0)),
        ("14:00-14:45", datetime(2026, 11, 5, 14, 0), datetime(2026, 11, 5, 14, 45)),
        ("20:00", datetime(2026, 11, 4, 20, 0), datetime(2026, 11, 5, 14, 45)),
    ],
)
def test_time_is_the_latest_that_has_ended(text, start, end):
    from noggin_compass.naps import parse_span

    assert parse_span(text, NOW) == (start, end)


def test_fell_asleep_and_woke_up(book):
    started = book.press(data(book.menu(), "Уснул сейчас"))
    assert started.text == 'Записываю сон вне кроватки с 14:45. Когда малыш проснётся, нажмите "Проснулся".'
    assert labels(started) == [["Проснулся", "Удалить"]]
    # Pressed again, or by the other parent: still one sleep.
    book.clock_value["now"] = NOW + timedelta(minutes=40)
    assert book.press("nap:sleep").text == "Спит вне кроватки с 14:45, уже 40 мин."
    menu = book.menu()
    assert menu.text == "Спит вне кроватки с 14:45, уже 40 мин."
    assert labels(menu) == [["Проснулся", "Удалить"], ["Изменить записанный сон"]]
    assert book.current_line() == "Спит вне кроватки уже 40 мин, с 14:45."
    assert labels(book.list()) == [["14:45-сейчас, 40 мин"]]
    woke = book.press(data(started, "Проснулся"))
    assert woke.text == "Записан сон вне кроватки: 14:45-15:25, 40 мин."
    assert stored(book) == [(datetime(2026, 11, 5, 14, 45), datetime(2026, 11, 5, 15, 25))]
    assert book.current_line() is None
    # The other parent's old message: the sleep is already logged.
    assert book.press(data(started, "Проснулся")).text == "14:45-15:25, 40 мин"


def test_waking_in_the_same_minute_logs_nothing(book):
    started = book.press("nap:sleep")
    assert book.press(data(started, "Проснулся")).text == "Сон меньше минуты не записан."
    assert stored(book) == []


def test_open_nap_counts_till_now(book):
    from noggin_compass.naps import periods

    book.press("nap:sleep")
    storage = Storage(book.db_path)
    try:
        (nap,) = periods(storage, now=NOW + timedelta(minutes=30))
    finally:
        storage.close()
    assert (nap.start, nap.end, nap.ongoing, nap.manual) == (
        datetime(2026, 11, 5, 14, 45), NOW + timedelta(minutes=30), True, True)


def test_warns_about_overlaps(book):
    from noggin_compass.head import HeadEstimate, Side

    storage = Storage(book.db_path)
    try:
        # The camera saw the baby still in the crib from 12:00 to 12:30.
        moment = datetime(2026, 11, 5, 12, 0)
        while moment < datetime(2026, 11, 5, 12, 30):
            storage.add(moment, HeadEstimate(Side.CENTER, 0.0, "test"), motion=0.0)
            moment += timedelta(seconds=6)
        storage.add_nap(datetime(2026, 11, 5, 11, 0), datetime(2026, 11, 5, 11, 40))
    finally:
        storage.close()
    reply = book.command("11:30-12:10")
    assert reply.text == (
        "Записан сон вне кроватки: 11:30-12:10, 40 мин.\n"
        "Пересекается со сном в кроватке: 12:00-12:10. Это время посчитано один раз.\n"
        "Пересекается с другой записью: 11:30-11:40. Это время посчитано один раз."
    )
    assert book.command("13:00-13:30").text == "Записан сон вне кроватки: 13:00-13:30, 30 мин."
