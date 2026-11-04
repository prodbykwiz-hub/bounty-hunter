"""Рабочая копия репозитория и песочница файловых операций для LLM.

Модель может только читать, искать и править файлы внутри клона.
Запуска команд нет намеренно: текст issue пишут посторонние люди, и через
prompt injection он не должен получить возможность выполнить код на ПК.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("bounty_agent")

GIT_TIMEOUT = 600
MAX_READ_LINES = 400
MAX_READ_BYTES = 60_000
MAX_LIST_ENTRIES = 300
MAX_SEARCH_LINES = 120
MAX_WRITE_BYTES = 400_000
# push на GitHub — через gh того аккаунта, под которым он залогинен, а не через
# сохранённые в Git Credential Manager учётные данные (там может быть другой аккаунт).
GH_CREDENTIAL_ARGS = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")


class GitError(Exception):
    pass


class SandboxError(Exception):
    pass


def git(cwd: Path, *args: str, env_args: tuple[str, ...] = ()) -> str:
    proc = subprocess.run(
        ["git", *env_args, *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "fix"


def prepare(work_dir: Path, repo: str, default_branch: str, branch: str,
            url: str | None = None) -> tuple[Path, str]:
    """Клонирует (или обновляет) репозиторий и создаёт ветку. Возвращает (путь, base sha)."""
    path = work_dir / repo.replace("/", "__")
    url = url or f"https://github.com/{repo}.git"
    if (path / ".git").exists():
        git(path, "fetch", "--depth", "50", "origin", default_branch)
        git(path, "checkout", "-B", branch, "FETCH_HEAD")
        # Папка — наша песочница: остатки прошлой попытки удаляем.
        git(path, "reset", "--hard", "FETCH_HEAD")
        git(path, "clean", "-fdx")
    else:
        if path.exists():
            shutil.rmtree(path)
        work_dir.mkdir(parents=True, exist_ok=True)
        git(work_dir, "clone", "--depth", "50", "--branch", default_branch, url, str(path))
        git(path, "checkout", "-b", branch)
    base = git(path, "rev-parse", "HEAD").strip()
    return path, base


def commit_all(path: Path, message: str, author_name: str, author_email: str) -> bool:
    git(path, "add", "-A")
    if not git(path, "status", "--porcelain").strip():
        return False
    git(path, "commit", "-m", message,
        env_args=("-c", f"user.name={author_name}", "-c", f"user.email={author_email}"))
    return True


def diff(path: Path, base: str) -> tuple[str, str]:
    """(stat, patch) изменений относительно base."""
    return git(path, "diff", "--stat", base, "HEAD"), git(path, "diff", base, "HEAD")


def push(path: Path, fork_full_name: str, branch: str) -> None:
    git(path, "push", "--force-with-lease", f"https://github.com/{fork_full_name}.git",
        f"HEAD:refs/heads/{branch}", env_args=GH_CREDENTIAL_ARGS)


class Workspace:
    """Файловые инструменты для function calling, ограниченные папкой клона."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.changed: set[str] = set()

    def _resolve(self, rel: str) -> Path:
        rel = (rel or ".").replace("\\", "/").lstrip("/")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise SandboxError(f"путь вне репозитория: {rel}")
        parts = target.relative_to(self.root).parts
        if parts and parts[0] == ".git":
            raise SandboxError("доступ к .git запрещён")
        return target

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def list_dir(self, path: str = ".") -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_dir():
            return {"error": f"не папка: {path}"}
        entries = sorted(
            (p.name + ("/" if p.is_dir() else "") for p in target.iterdir() if p.name != ".git"),
            key=lambda name: (not name.endswith("/"), name.lower()),
        )
        return {"path": self._rel(target), "entries": entries[:MAX_LIST_ENTRIES],
                "truncated": len(entries) > MAX_LIST_ENTRIES}

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"нет файла: {path}"}
        raw = target.read_bytes()
        if b"\0" in raw[:8000]:
            return {"error": "бинарный файл"}
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        return {"path": self._rel(target), "total_lines": len(lines), "from": start, "to": end,
                "content": body[:MAX_READ_BYTES]}

    def search(self, pattern: str, path: str = ".") -> dict[str, Any]:
        target = self._resolve(path)
        proc = subprocess.run(
            ["git", "grep", "-n", "-I", "-E", "-e", pattern, "--", self._rel(target)],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if proc.returncode not in (0, 1):
            return {"error": proc.stderr.strip()[:300]}
        lines = proc.stdout.splitlines()
        return {"matches": [line[:300] for line in lines[:MAX_SEARCH_LINES]], "truncated": len(lines) > MAX_SEARCH_LINES}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return {"error": "файл слишком большой"}
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        self.changed.add(self._rel(target))
        return {"ok": True, "path": self._rel(target)}

    def replace_in_file(self, path: str, old: str, new: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"нет файла: {path}"}
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old) if old else 0
        if count != 1:
            return {"error": f"фрагмент 'old' найден {count} раз, нужен ровно 1 — уточни фрагмент"}
        target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
        self.changed.add(self._rel(target))
        return {"ok": True, "path": self._rel(target)}

    def delete_file(self, path: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"нет файла: {path}"}
        target.unlink()
        self.changed.add(self._rel(target))
        return {"ok": True}

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tools = {
            "list_dir": self.list_dir,
            "read_file": self.read_file,
            "search": self.search,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "delete_file": self.delete_file,
        }
        tool = tools.get(name)
        if tool is None:
            return {"error": f"неизвестный инструмент {name}"}
        try:
            return tool(**args)
        except SandboxError as exc:
            return {"error": str(exc)}
        except (TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
