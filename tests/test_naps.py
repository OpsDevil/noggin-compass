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
    assert menu.text == "Сколько малыш спал вне кроватки? Сон закончился только что."
    assert labels(menu) == [
        ["15 мин", "30 мин", "45 мин", "1 ч"],
        ["1 ч 30 мин", "2 ч", "2 ч 30 мин", "3 ч"],
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
    assert edit.text == "Записано: 13:15-14:45, 1 ч 30 мин. Сколько малыш спал на самом деле?"
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
