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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from . import keychain
from .i18n import t

log = logging.getLogger(__name__)

TOKEN_ENV = "NOGGIN_TELEGRAM_TOKEN"
CHAT_IDS_ENV = "NOGGIN_TELEGRAM_CHAT_IDS"
KEYCHAIN_SERVICE = "noggin-compass-telegram"
API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
RETRY_DELAY = 10


@dataclass
class Photo:
    image: bytes  # JPEG or PNG, as the file name says
    caption: str = ""
    filename: str = "frame.jpg"
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, data), as in Reply

    @property
    def content_type(self) -> str:
        return "image/png" if self.filename.endswith(".png") else "image/jpeg"


@dataclass
class Reply:
    """A message with buttons under it. A press on a button comes back as its data, and the
    answer to it replaces the message, so old buttons do not linger."""

    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, data)

    def markup(self) -> str:
        rows = [[{"text": label, "callback_data": data} for label, data in row] for row in self.buttons]
        return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


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
        handler: Callable[[str], str | Photo | Reply | None],
        opener: Callable = urllib.request.urlopen,
        commands: list[tuple[str, str]] = (),
        on_button: Callable[[str], str | Reply | Photo | None] | None = None,
    ):
        self.token = token
        self.chat_ids = set(chat_ids)
        self.handler = handler  # command text -> reply, or None if there is nothing to say
        self.on_button = on_button  # button data -> what replaces the message with the button
        self.commands = list(commands)  # shown in the Telegram command menu
        self._open = opener
        self._running = False

    def _post(self, method: str, data: bytes, content_type: str) -> object:
        request = urllib.request.Request(
            API.format(token=self.token, method=method), data=data, headers={"Content-Type": content_type}
        )
        with self._open(request, timeout=POLL_TIMEOUT + 10) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram error"))
        return body["result"]

    def call(self, method: str, **params) -> object:
        data = urllib.parse.urlencode(params).encode()
        return self._post(method, data, "application/x-www-form-urlencoded")

    def send_photo(self, chat: int, photo: Photo) -> bool:
        boundary = uuid.uuid4().hex
        fields = [("chat_id", chat), ("caption", photo.caption)]
        if photo.buttons:
            fields.append(("reply_markup", Reply("", photo.buttons).markup()))
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{photo.filename}"\r\n'
            f"Content-Type: {photo.content_type}\r\n\r\n".encode() + photo.image + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        try:
            self._post("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        except Exception as error:
            log.warning(t("tg.error", error=error))
            return False
        return True

    def send(self, text: str | Reply, chat_ids: list[int] | None = None, silent: bool = False) -> None:
        """`silent` delivers it without a sound on the phone: for news that can wait till morning."""
        reply = text if isinstance(text, Reply) else Reply(text)
        extra = {"reply_markup": reply.markup()} if reply.buttons else {}
        if silent:
            extra["disable_notification"] = "true"
        for chat in chat_ids or sorted(self.chat_ids):
            try:
                self.call("sendMessage", chat_id=chat, text=reply.text, **extra)
            except Exception as error:  # no network etc.: the alert still sounds on the computer
                log.warning(t("tg.error", error=error))

    def _press(self, query: dict) -> None:
        message = query.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        try:
            # Stops the spinner on the button.
            self.call("answerCallbackQuery", callback_query_id=query.get("id", ""))
        except Exception as error:
            log.warning(t("tg.error", error=error))
        if chat not in self.chat_ids:
            log.info(t("tg.unknown_chat", chat=chat))
            return
        reply = self.on_button(query.get("data") or "") if self.on_button else None
        if reply is None:
            return
        # A picture always comes as a new message, and so does any answer to a button under a
        # picture: a picture's message has no text to replace.
        if isinstance(reply, Photo):
            if not self.send_photo(chat, reply):
                self.send(t("tg.photo_failed"), [chat])
            return
        if "photo" in message:
            self.send(reply, [chat])
            return
        reply = reply if isinstance(reply, Reply) else Reply(reply)
        try:
            self.call(
                "editMessageText",
                chat_id=chat,
                message_id=message.get("message_id"),
                text=reply.text,
                reply_markup=reply.markup(),
            )
        except Exception as error:
            log.warning(t("tg.error", error=error))

    def handle_updates(self, updates: list[dict]) -> int | None:
        """Answer commands from allowed chats and return the next offset. An update that fails is
        logged and passed over: otherwise it would come back with every poll, block all the
        commands after it, and run the ones before it in the batch again."""
        offset = None
        for update in updates:
            offset = update["update_id"] + 1
            try:
                self._handle(update)
            except Exception:
                log.exception(t("tg.update_failed"))
                chat = ((update.get("message") or (update.get("callback_query") or {}).get("message") or {})
                        .get("chat") or {}).get("id")
                if chat in self.chat_ids:
                    self.send(t("tg.command_failed"), [chat])
        return offset

    def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            self._press(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        text = message.get("text") or ""
        if chat is None or not text:
            return
        if chat not in self.chat_ids:
            log.info(t("tg.unknown_chat", chat=chat))
            return
        reply = self.handler(text)
        if isinstance(reply, Photo):
            if not self.send_photo(chat, reply):
                self.send(t("tg.photo_failed"), [chat])
        elif reply:
            self.send(reply, [chat])

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
        if self.commands:
            try:
                menu = [{"command": name, "description": text} for name, text in self.commands]
                self.call("setMyCommands", commands=json.dumps(menu, ensure_ascii=False))
            except Exception as error:
                log.warning(t("tg.error", error=error))
        threading.Thread(target=self._poll, daemon=True).start()

    def stop(self) -> None:
        self._running = False
