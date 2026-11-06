"""Тексты сообщений: Gemini, а если он недоступен — Claude Code CLI без инструментов.

Google закрывает Gemini API для части стран («User location is not supported»),
поэтому без запасного варианта агент там не может написать ни строчки.
Код этот модуль не пишет: для кода есть agent.coder.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from agent.gemini import Gemini, GeminiError

log = logging.getLogger("bounty_agent")

JSON_HINT = "\n\nreply with a single json object only, no markdown fences, no extra text."


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip().removeprefix("```json").strip().strip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GeminiError(f"ответ не JSON: {raw[:200]}") from exc
    if not isinstance(data, dict):
        raise GeminiError(f"ожидался JSON-объект: {raw[:200]}")
    return data


@dataclass(frozen=True)
class ClaudeWriter:
    """Claude Code CLI в режиме «только текст»: без инструментов, MCP и чужих настроек."""

    binary: str = "claude"
    model: str = "sonnet"
    timeout: int = 180

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def command(self, system: str) -> list[str]:
        cmd = [
            self.binary, "-p",
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "user",
            "--no-session-persistence",
            "--system-prompt", system,
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def text(self, system: str, prompt: str) -> str:
        # Пустая временная папка: CLAUDE.md и файлы проекта в контекст не попадают.
        with tempfile.TemporaryDirectory(prefix="bounty-writer-") as cwd:
            try:
                proc = subprocess.run(
                    self.command(system), input=prompt, cwd=cwd, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=self.timeout,
                )
            except FileNotFoundError as exc:
                raise GeminiError(f"не найден {self.binary}") from exc
            except subprocess.TimeoutExpired as exc:
                raise GeminiError(f"claude не ответил за {self.timeout} с") from exc
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            tail = (proc.stderr or proc.stdout).strip()[-300:]
            raise GeminiError(f"claude завершился с кодом {proc.returncode}: {tail}") from exc
        result = str((data or {}).get("result") or "").strip() if isinstance(data, dict) else ""
        if not isinstance(data, dict) or data.get("is_error") or not result:
            raise GeminiError(f"claude: {result[:300] or 'пустой ответ'}")
        return result

    def json(self, system: str, prompt: str) -> dict[str, Any]:
        return parse_json_object(self.text(system + JSON_HINT, prompt))


class Writer:
    """Пишет через Gemini; при любой ошибке Gemini повторяет через Claude.

    Если Gemini недоступен из-за региона, дальше в этом запуске сразу идём в Claude,
    чтобы не ждать заведомо неудачный запрос.
    """

    def __init__(self, gemini: Gemini | None, claude: ClaudeWriter | None) -> None:
        self.gemini = gemini
        self.claude = claude
        self._gemini_blocked = False
        self.last_author = ""

    def _run(self, method: str, system: str, prompt: str) -> Any:
        errors: list[str] = []
        if self.gemini is not None and not self._gemini_blocked:
            try:
                result = getattr(self.gemini, method)(system, prompt)
                self.last_author = "gemini"
                return result
            except GeminiError as exc:
                if "location is not supported" in str(exc).lower():
                    self._gemini_blocked = True
                    log.warning("Gemini недоступен в этом регионе — тексты пишет Claude")
                else:
                    log.warning("Gemini: %s — пробую Claude", exc)
                errors.append(f"gemini: {str(exc)[:200]}")
        if self.claude is not None:
            try:
                result = getattr(self.claude, method)(system, prompt)
                self.last_author = "claude"
                return result
            except GeminiError as exc:
                errors.append(str(exc)[:200])
        raise GeminiError("; ".join(errors) or "нет доступной модели для текстов")

    def text(self, system: str, prompt: str) -> str:
        return self._run("text", system, prompt)

    def json(self, system: str, prompt: str) -> dict[str, Any]:
        return self._run("json", system, prompt)
