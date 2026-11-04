"""Telegram bot: sends messages and takes commands via the Bot API, standard library only.
The token never reaches the logs: urllib errors don't include it, and the request URL is never
printed."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import keychain
from .i18n import t

log = logging.getLogger(__name__)

TOKEN_ENV = "NOGGIN_TELEGRAM_TOKEN"
CHAT_IDS_ENV = "NOGGIN_TELEGRAM_CHAT_IDS"
KEYCHAIN_SERVICE = "noggin-compass-telegram"
API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
RETRY_DELAY = 10


def resolve_credentials(token: str | None, chat_ids: list[int]) -> tuple[str | None, list[int]]:
    """Token lookup order: the environment variable (including .env), the config, the macOS
    Keychain. Chat ids: the environment variable, then the config."""
    token = os.environ.get(TOKEN_ENV) or token or keychain.read(KEYCHAIN_SERVICE)
    env_ids = os.environ.get(CHAT_IDS_ENV, "")
    ids = [int(part) for part in env_ids.replace(" ", "").split(",") if part] or list(chat_ids)
    return token, ids


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_ids: list[int],
        handler: Callable[[str], str | None],
        opener: Callable = urllib.request.urlopen,
    ):
        self.token = token
        self.chat_ids = set(chat_ids)
        self.handler = handler  # command text -> reply, or None if there is nothing to say
        self._open = opener
        self._running = False

    def call(self, method: str, **params) -> object:
        data = urllib.parse.urlencode(params).encode()
        url = API.format(token=self.token, method=method)
        with self._open(url, data=data, timeout=POLL_TIMEOUT + 10) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram error"))
        return body["result"]

    def send(self, text: str, chat_ids: list[int] | None = None) -> None:
        for chat in chat_ids or sorted(self.chat_ids):
            try:
                self.call("sendMessage", chat_id=chat, text=text)
            except Exception as error:  # no network etc.: the alert still sounds on the computer
                log.warning(t("tg.error", error=error))

    def handle_updates(self, updates: list[dict]) -> int | None:
        """Answer commands from allowed chats and return the next offset."""
        offset = None
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = (message.get("chat") or {}).get("id")
            text = message.get("text") or ""
            if chat is None or not text:
                continue
            if chat not in self.chat_ids:
                log.info(t("tg.unknown_chat", chat=chat))
                continue
            reply = self.handler(text)
            if reply:
                self.send(reply, [chat])
        return offset

    def _poll(self) -> None:
        offset = None
        while self._running:
            try:
                params = {"timeout": POLL_TIMEOUT}
                if offset is not None:
                    params["offset"] = offset
                offset = self.handle_updates(self.call("getUpdates", **params)) or offset
            except Exception as error:
                log.warning(t("tg.error", error=error))
                time.sleep(RETRY_DELAY)

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self) -> None:
        self._running = False
