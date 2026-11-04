"""Состояние взятых задач. Хранится локально в agent_state.json (в git не попадает)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("bounty_agent")

# Жизненный цикл задачи:
# drafting → awaiting_post → posted → working → review → pr_opened
# На любом шаге задача может стать dropped.
DRAFTING = "drafting"
AWAITING_POST = "awaiting_post"
POSTED = "posted"
WORKING = "working"
REVIEW = "review"
PR_OPENED = "pr_opened"
DROPPED = "dropped"
ACTIVE_STAGES = frozenset({DRAFTING, AWAITING_POST, POSTED, WORKING, REVIEW})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Task:
    key: str
    repo: str
    number: int
    title: str
    url: str
    stage: str = DRAFTING
    draft: str = ""
    # "offer" — первое предложение взять задачу, "reply" — ответ на вопрос мейнтейнера.
    draft_mode: str = "offer"
    reply_to_comment: str = ""
    ai_policy: str = ""
    # Сообщения в Telegram, на которые можно ответить текстом-правкой.
    message_ids: list[int] = field(default_factory=list)
    comment_id: int | None = None
    comment_url: str = ""
    posted_at: str = ""
    reported_comment_ids: list[int] = field(default_factory=list)
    branch: str = ""
    workdir: str = ""
    base_sha: str = ""
    commit_message: str = ""
    summary: str = ""
    instructions: list[str] = field(default_factory=list)
    pr_url: str = ""
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tasks: dict[str, Task] = {}
        # Offset Telegram getUpdates: без него после перезапуска старые нажатия придут снова.
        self.offset: int | None = None
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.tasks = {key: Task.from_dict(value) for key, value in raw.get("tasks", {}).items()}
                self.offset = raw.get("offset")
            except (OSError, ValueError, TypeError) as exc:
                backup = path.with_suffix(path.suffix + ".corrupt")
                log.error("Состояние агента повреждено (%s), копия в %s", exc, backup)
                path.replace(backup)
        log.info("Задач в работе: %d", sum(t.stage in ACTIVE_STAGES for t in self.tasks.values()))

    def get(self, key: str) -> Task | None:
        return self.tasks.get(key)

    def put(self, task: Task) -> None:
        task.updated_at = utc_now()
        self.tasks[task.key] = task
        self.save()

    def by_stage(self, *stages: str) -> list[Task]:
        return [task for task in self.tasks.values() if task.stage in stages]

    def by_message(self, message_id: int) -> Task | None:
        for task in self.tasks.values():
            if message_id in task.message_ids and task.stage in ACTIVE_STAGES:
                return task
        return None

    def save(self) -> None:
        payload = {
            "version": 1,
            "offset": self.offset,
            "tasks": {key: asdict(task) for key, task in sorted(self.tasks.items())},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
