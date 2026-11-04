import pytest

from noggin_compass.i18n import set_language


@pytest.fixture(autouse=True)
def english_by_default():
    set_language("en")
    yield
    set_language("en")
