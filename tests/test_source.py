import os
import subprocess

import pytest

from noggin_compass import keychain, source
from noggin_compass.config import load_dotenv

URL = "rtsp://cam:{password}@192.168.1.50:554/stream1"


@pytest.fixture(autouse=True)
def no_password_sources(monkeypatch):
    # No environment variable and no real Keychain: each test sets up its own.
    monkeypatch.delenv(source.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(keychain.subprocess, "run", fake_keychain(44))


def fake_keychain(returncode: int, stdout: str = ""):
    def run(args, **kwargs):
        assert args[:2] == ["security", "find-generic-password"]
        assert source.KEYCHAIN_SERVICE in args
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return run


def test_password_from_config_is_url_encoded():
    assert source.resolve_password(URL, "p@ss:w/rd") == "rtsp://cam:p%40ss%3Aw%2Frd@192.168.1.50:554/stream1"


def test_environment_variable_wins_over_config(monkeypatch):
    monkeypatch.setenv(source.PASSWORD_ENV, "from-env")
    assert source.resolve_password(URL, "from-config") == "rtsp://cam:from-env@192.168.1.50:554/stream1"


def test_empty_environment_variable_is_ignored(monkeypatch):
    monkeypatch.setenv(source.PASSWORD_ENV, "")
    assert source.resolve_password(URL, "from-config") == "rtsp://cam:from-config@192.168.1.50:554/stream1"


def test_config_wins_over_keychain(monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", fake_keychain(0, "from-keychain\n"))
    assert source.resolve_password(URL, "from-config") == "rtsp://cam:from-config@192.168.1.50:554/stream1"


def test_keychain_is_the_last_resort(monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", fake_keychain(0, "from-keychain\n"))
    assert source.resolve_password(URL) == "rtsp://cam:from-keychain@192.168.1.50:554/stream1"


def test_no_keychain_outside_macos(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(keychain.subprocess, "run", missing)
    with pytest.raises(RuntimeError):
        source.resolve_password(URL)


def test_missing_password_explains_all_options():
    with pytest.raises(RuntimeError, match=source.PASSWORD_ENV) as error:
        source.resolve_password(URL)
    assert "camera.password" in str(error.value)
    assert source.KEYCHAIN_SERVICE in str(error.value)


def test_sources_without_placeholder_are_untouched():
    assert source.resolve_password(0) == 0
    assert source.resolve_password("rtsp://192.168.1.50/stream1") == "rtsp://192.168.1.50/stream1"


def test_dotenv(tmp_path, monkeypatch):
    for key in ("A", "B", "C", "D", "E"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("E", "already set")
    env = tmp_path / ".env"
    env.write_text('# comment\nA=plain\nexport B="quoted value"\nC=has#hash\nD=\nE=from file\n', encoding="utf-8")
    load_dotenv(env)

    assert os.environ["A"] == "plain"
    assert os.environ["B"] == "quoted value"
    assert os.environ["C"] == "has#hash"
    assert "D" not in os.environ
    assert os.environ["E"] == "already set"
    load_dotenv(tmp_path / "missing.env")  # a missing file is a no-op
