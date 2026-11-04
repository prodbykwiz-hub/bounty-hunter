"""Telegram Bot API: long polling, кнопки, документы."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger("bounty_agent")

API = "https://api.telegram.org"
POLL_TIMEOUT = 30
MAX_RETRIES = 4
MAX_TEXT = 4096


class TelegramError(Exception):
    pass


def keyboard(*rows: list[tuple[str, str]]) -> dict[str, Any]:
    """keyboard([("текст", "callback"), ...], ...) → inline_keyboard."""
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


class Bot:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***")

    def _call(self, method: str, params: dict[str, Any], http_timeout: int = 30,
              files: dict | None = None) -> Any:
        # Параметры API передаём словарём: у getUpdates есть свой "timeout" (long polling),
        # и он не должен смешиваться с таймаутом HTTP-запроса.
        url = f"{API}/bot{self.token}/{method}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if files:
                    resp = self.session.post(url, data=params, files=files, timeout=http_timeout)
                else:
                    resp = self.session.post(url, json=params, timeout=http_timeout)
            except requests.RequestException as exc:
                wait = 2**attempt
                log.warning("Telegram %s: %s, повтор через %d с", method, self._redact(str(exc)), wait)
                time.sleep(wait)
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 429:
                wait = int((data.get("parameters") or {}).get("retry_after") or 5) + 1
                log.warning("Telegram flood control, жду %d с", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if not data.get("ok"):
                raise TelegramError(f"{method}: HTTP {resp.status_code}: {data.get('description', resp.text[:200])}")
            return data.get("result")
        raise TelegramError(f"{method}: не удалось после {MAX_RETRIES} попыток")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, http_timeout=POLL_TIMEOUT + 15) or []

    def send(self, text: str, markup: dict[str, Any] | None = None, reply_to: int | None = None) -> int:
        params: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:MAX_TEXT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            params["reply_markup"] = markup
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", params)["message_id"]

    def send_document(self, filename: str, content: bytes, caption: str = "") -> int:
        result = self._call(
            "sendDocument",
            {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
            http_timeout=60,
            files={"document": (filename, content)},
        )
        return result["message_id"]

    def clear_buttons(self, message_id: int) -> None:
        try:
            self._call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id,
                                                  "reply_markup": {"inline_keyboard": []}})
        except TelegramError as exc:
            # "message is not modified" и старые сообщения — не критично.
            log.debug("Не удалось убрать кнопки: %s", exc)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
        except TelegramError as exc:
            log.debug("answerCallbackQuery: %s", exc)
