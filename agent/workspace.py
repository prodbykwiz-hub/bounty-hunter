"""Рабочая копия репозитория: клон, ветка, коммит, diff, push."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("bounty_agent")

GIT_TIMEOUT = 600
# push на GitHub — через gh того аккаунта, под которым он залогинен, а не через
# сохранённые в Git Credential Manager учётные данные (там может быть другой аккаунт).
GH_CREDENTIAL_ARGS = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")


class GitError(Exception):
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
