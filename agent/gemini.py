"""Клиент Gemini (REST generateContent). Только тексты сообщений — код пишет Claude."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger("bounty_agent")

API = "https://generativelanguage.googleapis.com/v1beta/models"
# Flash отвечает за секунды; долго ждать незачем — при сбое тексты пишет Claude (agent.writer).
REQUEST_TIMEOUT = 90
MAX_RETRIES = 3
ZERO_QUOTA_RE = re.compile(r"limit:\s*0\b")


class GeminiError(Exception):
    pass


class Gemini:
    def __init__(self, api_key: str, model: str, proxy: str = "") -> None:
        self.model = model
        self.session = requests.Session()
        self.session.headers.update({"x-goog-api-key": api_key, "Content-Type": "application/json"})
        if proxy:
            # Прокси только для Gemini: Google закрывает API для части регионов.
            self.session.proxies.update({"https": proxy, "http": proxy})

    def generate(self, system: str, contents: list[dict[str, Any]], json_mode: bool = False) -> dict[str, Any]:
        """Возвращает content ответа модели ({"role": "model", "parts": [...]})."""
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
        }
        if json_mode:
            body["generationConfig"] = {"responseMimeType": "application/json"}

        url = f"{API}/{self.model}:generateContent"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                wait = 5 * attempt
                # В тексте исключения нет ключа: он передаётся заголовком, а не в URL.
                log.warning("Gemini: %s, повтор через %d с", type(exc).__name__, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429 and is_zero_quota(resp.text):
                # Повтор не поможет: модель не входит в тариф (например, Pro на бесплатном).
                raise GeminiError(
                    f"у ключа нет квоты на {self.model}: на бесплатном тарифе она 0. "
                    "подключи оплату в aistudio.google.com или поставь GEMINI_MODEL=gemini-3.8-flash"
                )
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 5 * attempt
                log.warning("Gemini: HTTP %d, повтор через %d с", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise GeminiError(f"HTTP {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            block = (data.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise GeminiError(f"запрос заблокирован: {block}")
            candidates = data.get("candidates") or []
            if not candidates or not (candidates[0].get("content") or {}).get("parts"):
                reason = candidates[0].get("finishReason") if candidates else "нет кандидатов"
                raise GeminiError(f"пустой ответ ({reason})")
            content = candidates[0]["content"]
            content.setdefault("role", "model")
            return content
        raise GeminiError(f"не удалось после {MAX_RETRIES} попыток")

    def text(self, system: str, prompt: str) -> str:
        content = self.generate(system, [{"role": "user", "parts": [{"text": prompt}]}])
        return response_text(content)

    def json(self, system: str, prompt: str) -> dict[str, Any]:
        content = self.generate(system, [{"role": "user", "parts": [{"text": prompt}]}], json_mode=True)
        raw = response_text(content).strip().removeprefix("```json").strip("`").strip()
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise GeminiError(f"ответ не JSON: {raw[:200]}") from exc
        if not isinstance(data, dict):
            raise GeminiError(f"ожидался JSON-объект: {raw[:200]}")
        return data


def is_zero_quota(body: str) -> bool:
    return bool(ZERO_QUOTA_RE.search(body))


def response_text(content: dict[str, Any]) -> str:
    # Части с thought=True — внутренние размышления модели, в ответ их не берём.
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
