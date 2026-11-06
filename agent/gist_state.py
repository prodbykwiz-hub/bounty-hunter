"""Копия agent_state.json в секретном gist: для хостинга без постоянного диска.

При старте файл восстанавливается из gist, дальше фоновый поток раз в минуту
отправляет изменения, а при остановке (SIGTERM от Render) — последнюю версию.
Gist ищется по описанию, поэтому его id нигде настраивать не нужно.
Секретный gist не виден в профиле, но открыт любому, у кого есть ссылка:
токенов и ключей в состоянии нет — только задачи, черновики и id сообщений.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("bounty_agent")

DESCRIPTION = "bounty-agent state (managed by the agent, do not edit)"
FILENAME = "agent_state.json"
SYNC_INTERVAL = 60


class GistState:
    def __init__(self, gh: Any, path: Path) -> None:
        self.gh = gh
        self.path = path
        self.gist_id = ""
        self._pushed = ""
        self._lock = threading.Lock()

    def _find(self) -> dict[str, Any] | None:
        for page in range(1, 6):
            gists = self.gh.api(f"gists?per_page=100&page={page}") or []
            for gist in gists:
                if gist.get("description") == DESCRIPTION:
                    return gist
            if len(gists) < 100:
                break
        return None

    def restore(self) -> None:
        """Находит gist (или создаёт) и кладёт его содержимое в локальный файл."""
        gist = self._find()
        if gist is None:
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else '{"version": 1, "tasks": {}}\n'
            created = self.gh.api("gists", method="POST", payload={
                "description": DESCRIPTION, "public": False, "files": {FILENAME: {"content": text}},
            })
            self.gist_id, self._pushed = created["id"], text
            log.info("Создал секретный gist для состояния агента")
            return
        self.gist_id = gist["id"]
        full = self.gh.api(f"gists/{self.gist_id}")
        file = ((full or {}).get("files") or {}).get(FILENAME) or {}
        text = file.get("content") or ""
        if file.get("truncated"):
            text = requests.get(file["raw_url"], timeout=60).text
        if text.strip():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
            self._pushed = text
            log.info("Состояние агента восстановлено из gist")

    def push(self) -> None:
        with self._lock:
            if not self.gist_id or not self.path.exists():
                return
            text = self.path.read_text(encoding="utf-8")
            if text == self._pushed:
                return
            self.gh.api(f"gists/{self.gist_id}", method="PATCH",
                        payload={"files": {FILENAME: {"content": text}}})
            self._pushed = text

    def _loop(self, stop: threading.Event) -> None:
        while not stop.wait(SYNC_INTERVAL):
            try:
                self.push()
            except Exception as exc:  # noqa: BLE001 — сбой синхронизации не должен ронять агента
                log.warning("Не удалось сохранить состояние в gist: %s", exc)

    def start(self) -> threading.Event:
        stop = threading.Event()
        threading.Thread(target=self._loop, args=(stop,), name="gist-sync", daemon=True).start()
        return stop
