"""Ежедневный отчёт в Telegram: сканер, задачи, кто писал тексты, ошибки."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.state import ACTIVE_STAGES, DROPPED, PR_OPENED, Task

esc = html.escape

STAGE_NAMES = {
    "drafting": "пишу черновик",
    "awaiting_post": "ждёт отправки",
    "posted": "жду мейнтейнера",
    "working": "claude пишет код",
    "review": "ждёт твоего ревью",
}


def _name(task: Task) -> str:
    if task.kind == "gig":
        title = task.title if len(task.title) <= 50 else task.title[:49] + "…"
        return f"{task.repo}: {title}"
    return f"{task.repo}#{task.number}"


def is_due(now: datetime, hour: int | None, last_report: str) -> bool:
    """Пора ли слать отчёт: наступил час hour (UTC) и сегодня отчёта ещё не было."""
    if hour is None:
        return False
    return now.hour >= hour and last_report != now.strftime("%Y-%m-%d")


def _changed_since(task: Task, since: datetime) -> bool:
    try:
        return datetime.fromisoformat(task.updated_at.replace("Z", "+00:00")) >= since
    except ValueError:
        return False


def _uptime(seconds: float) -> str:
    hours = int(seconds // 3600)
    return f"{hours // 24} д {hours % 24} ч" if hours >= 24 else f"{hours} ч {int(seconds % 3600 // 60)} мин"


def build(tasks: list[Task], stats: dict[str, int], scan: dict[str, int] | None,
          seen: dict[str, int] | None, uptime_seconds: float, now: datetime,
          hours: int = 24) -> str:
    since = now - timedelta(hours=hours)
    lines = [f"📊 <b>отчёт за {hours} ч</b>"]

    if scan is None:
        lines.append("• сканер: не смог получить данные")
    else:
        failed = f", с ошибкой {scan['failed']}" if scan.get("failed") else ""
        lines.append(f"• сканер: {scan['total']} запусков{failed}")
    if seen is not None:
        sent, queued = seen.get("sent", 0), seen.get("queued", 0)
        lines.append(f"• разобрано issues: {sum(seen.values())}, прислано карточек: {sent}"
                     + (f", в очередь агенту: {queued}" if queued else ""))

    recent = [t for t in tasks if _changed_since(t, since)]
    # Отправленные ответы считаем счётчиком: после согласия задача уходит из стадии posted.
    posted = stats.get("posted", 0)
    prs = sum(t.stage == PR_OPENED for t in recent)
    dropped = sum(t.stage == DROPPED for t in recent)
    lines.append(f"• за сутки: ответов отправлено {posted}, PR открыто {prs}, снято задач {dropped}")

    active = [t for t in tasks if t.stage in ACTIVE_STAGES]
    if active:
        lines.append(f"• в работе {len(active)}:")
        lines += [f'   – <a href="{esc(t.url)}">{esc(_name(t))}</a>: '
                  f"{STAGE_NAMES.get(t.stage, t.stage)}" for t in active]
    else:
        lines.append("• задач в работе нет")

    gig_drafts, gig_sent = stats.get("gig_drafts", 0), stats.get("gig_sent", 0)
    if gig_drafts or gig_sent:
        lines.append(f"• заказы: черновиков откликов {gig_drafts}, ты отправил {gig_sent}")

    gemini, claude = stats.get("texts_gemini", 0), stats.get("texts_claude", 0)
    if gemini or claude:
        lines.append(f"• тексты писали: gemini {gemini}, claude {claude}")
    errors = stats.get("errors", 0)
    lines.append(f"• ошибок агента: {errors}" if errors else "• ошибок не было")
    lines.append(f"• агент работает {_uptime(uptime_seconds)} с последнего запуска")
    return "\n".join(lines)


def collect(gh: Any, scan_repo: str, workflow: str, now: datetime, hours: int = 24
            ) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """Данные сканера с GitHub; при сбое — None, отчёт всё равно уйдёт."""
    if not scan_repo:
        return None, None
    since = now - timedelta(hours=hours)
    try:
        scan = gh.scan_runs(scan_repo, workflow, since)
    except Exception:  # noqa: BLE001 — отчёт важнее точности
        scan = None
    try:
        seen = gh.seen_counts(scan_repo, "seen_issues.json", since)
    except Exception:  # noqa: BLE001
        seen = None
    return scan, seen


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
