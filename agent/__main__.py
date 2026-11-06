"""Точка входа: python -m agent"""

from __future__ import annotations

import html
import logging
import shutil
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agent import coder, keepalive, prompts, report, workspace
from agent.gist_state import GistState
from agent.config import AgentConfig, ConfigError
from agent.gemini import Gemini, GeminiError
from agent.github import AccountMismatch, GhError, GitHub
from agent.writer import ClaudeWriter, Writer
from agent.state import (
    ACTIVE_STAGES, AWAITING_POST, DRAFTING, DROPPED, POSTED, PR_OPENED, REVIEW, WORKING, Task, TaskStore,
    utc_now,
)
from agent.telegram import Bot, TelegramError, keyboard

log = logging.getLogger("bounty_agent")
esc = html.escape

MAINTAINER_ROLES = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
WATCH_GIVE_UP_DAYS = 14

CALLBACK_ACK = {
    "take": "пишу черновик…",
    "skip": "пропущено",
    "post": "отправляю…",
    "redo": "переписываю…",
    "drop": "отменено",
    "work": "запускаю…",
    "pr": "открываю PR…",
}


def clean_draft(text: str) -> str:
    """Снимает обёртки, которые модель иногда добавляет: кавычки и ```-блок (в любом порядке)."""
    text = text.strip()
    for _ in range(2):
        text = text.strip().strip('"').strip()
        if len(text) >= 6 and text.startswith("```") and text.endswith("```"):
            first, _, rest = text[3:-3].partition("\n")
            # Первая строка блока — пусто или тег языка ("text"), её выкидываем.
            text = rest if " " not in first.strip() else text[3:-3]
    return text.strip()


def issue_link(task: Task) -> str:
    return f'<a href="{esc(task.url)}">{esc(task.repo)}#{task.number}</a>'


class App:
    def __init__(self, cfg: AgentConfig, bot: Bot, gemini: Gemini, gh: GitHub, store: TaskStore,
                 claude: coder.ClaudeCoder | None = None) -> None:
        self.cfg = cfg
        self.bot = bot
        # Gemini пишет только сообщения; код — только Claude.
        self.gemini = gemini
        self.claude = claude or coder.ClaudeCoder(cfg.claude_bin, cfg.claude_model, cfg.coder_timeout)
        self.gh = gh
        self.store = store
        self.started = time.monotonic()
        self.callbacks: dict[str, Callable[[str], None]] = {
            "take": self.on_take,
            "skip": self.on_skip,
            "post": self.on_post,
            "redo": self.on_redo,
            "drop": self.on_drop,
            "work": self.on_work,
            "pr": self.on_pr,
        }

    # ------------------------------------------------------------ loop

    def run_forever(self) -> None:
        self.gh.ensure_account()
        log.info("Агент запущен: gh=%s, модель=%s", self.cfg.github_user, self.cfg.gemini_model)
        active = len(self.store.by_stage(*ACTIVE_STAGES))
        self.bot.send(f"🟢 агент запущен, задач в работе: {active}. /status — задачи, /report — отчёт")
        # Работа, прерванная перезапуском агента, сама не продолжится — предлагаем перезапуск.
        for task in self.store.by_stage(WORKING):
            self._offer_retry(task, "агент перезапускался, работа прервана")
        # Черновик, не дописанный из‑за перезапуска или сбоя Gemini, тоже предлагаем повторить.
        for task in self.store.by_stage(DRAFTING):
            task.message_ids.append(self.bot.send(
                f"✍️ черновик для {issue_link(task)} не готов, агент перезапускался",
                keyboard([("🔁 написать", f"redo:{task.key}"), ("отмена", f"drop:{task.key}")]),
            ))
            self.store.put(task)
        next_watch = next_scan = 0.0
        while True:
            if self.cfg.scan_repo and time.monotonic() >= next_scan:
                next_scan = time.monotonic() + self.cfg.scan_interval
                try:
                    # Пропуск, если сканер недавно отработал по cron: запас 2 минуты на задержки.
                    self.gh.trigger_scan(self.cfg.scan_repo, self.cfg.scan_workflow, self.cfg.scan_interval - 120)
                except Exception as exc:  # noqa: BLE001 — сбой запуска не должен ронять агента
                    log.warning("Не удалось запустить сканер: %s", exc)
            if report.is_due(report.utc_now(), self.cfg.report_hour, self.store.last_report):
                self.send_report(daily=True)
            if time.monotonic() >= next_watch:
                next_watch = time.monotonic() + self.cfg.watch_interval
                try:
                    self.watch()
                except Exception as exc:  # noqa: BLE001
                    log.exception("Ошибка наблюдателя")
                    self.notify_error(exc)
            try:
                updates = self.bot.get_updates(self.store.offset)
            except TelegramError as exc:
                log.error("getUpdates: %s", exc)
                time.sleep(10)
                continue
            for update in updates:
                # Offset сохраняем до обработки: упавший апдейт не должен крутиться бесконечно.
                self.store.offset = update["update_id"] + 1
                self.store.save()
                try:
                    self.handle_update(update)
                except Exception as exc:  # noqa: BLE001 — один апдейт не должен ронять агента
                    log.exception("Ошибка обработки апдейта")
                    self.notify_error(exc)

    def send_report(self, daily: bool = False) -> None:
        now = report.utc_now()
        scan, seen = report.collect(self.gh, self.cfg.scan_repo, self.cfg.scan_workflow, now)
        text = report.build(list(self.store.tasks.values()), self.store.stats, scan, seen,
                            time.monotonic() - self.started, now)
        try:
            self.bot.send(text)
        except TelegramError:
            log.exception("Отчёт не отправился")
        if daily:
            # Дату ставим и при сбое отправки: иначе отчёт пытался бы уйти на каждом круге цикла.
            self.store.last_report = now.strftime("%Y-%m-%d")
            self.store.stats = {}
            self.store.save()

    def notify_error(self, exc: Exception) -> None:
        self.store.bump("errors")
        try:
            self.bot.send(f"⚠️ ошибка: <code>{esc(str(exc)[:500])}</code>")
        except TelegramError:
            log.exception("Не удалось отправить ошибку в Telegram")

    def _authorized(self, user_id: Any, chat_id: Any) -> bool:
        return user_id == self.cfg.tg_owner_id and str(chat_id) == self.cfg.tg_chat_id

    def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            message = callback.get("message") or {}
            if not self._authorized((callback.get("from") or {}).get("id"), (message.get("chat") or {}).get("id")):
                log.warning("Нажатие от чужого пользователя %s проигнорировано", (callback.get("from") or {}).get("id"))
                self.bot.answer_callback(callback["id"], "нет доступа")
                return
            action, _, key = (callback.get("data") or "").partition(":")
            handler = self.callbacks.get(action)
            self.bot.answer_callback(callback["id"], CALLBACK_ACK.get(action, ""))
            if handler is None or not key:
                log.warning("Неизвестная кнопка %r", callback.get("data"))
                return
            if message.get("message_id"):
                self.bot.clear_buttons(message["message_id"])
            handler(key)
            return

        message = update.get("message") or {}
        if not message or not self._authorized((message.get("from") or {}).get("id"), (message.get("chat") or {}).get("id")):
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/status"):
            self.on_status()
            return
        if text.startswith("/report"):
            self.send_report()
            return
        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        task = self.store.by_message(reply_to) if reply_to else None
        if task:
            self.on_instruction(task, text)

    # -------------------------------------------------------- handlers

    def on_take(self, key: str) -> None:
        existing = self.store.get(key)
        if existing and existing.stage in ACTIVE_STAGES:
            self.bot.send(f"уже в работе: {issue_link(existing)} ({existing.stage})")
            return
        issue = self.gh.issue(key)
        if issue is None:
            self.bot.send("не нашёл этот issue, возможно его удалили")
            return
        task = Task(key=key, repo=issue["repo"], number=issue["number"], title=issue["title"], url=issue["url"])
        if issue["state"] != "OPEN":
            self.bot.send(f"{issue_link(task)} уже закрыт")
            return
        others = [a for a in issue["assignees"] if a.lower() != self.cfg.github_user.lower()]
        if others:
            self.bot.send(f"{issue_link(task)} уже назначен на {esc(', '.join(others))}")
            return
        task.ai_policy = self.gh.ai_policy(task.repo)
        try:
            task.approved_examples = self.gh.approved_applications(task.repo, task.number, self.cfg.github_user)
        except GhError as exc:
            log.warning("Не нашёл одобренных откликов в %s: %s", task.repo, exc)
        self.store.put(task)
        self.write_draft(task, issue)

    def write_draft(self, task: Task, issue: dict[str, Any]) -> None:
        style = prompts.load_style(self.cfg.style_file)
        if task.draft_mode == "reply":
            system = prompts.reply_system(style)
            prompt = prompts.reply_prompt(issue, task.reply_to_comment, task.instructions)
        else:
            system = prompts.comment_system(style)
            prompt = prompts.comment_prompt(issue, task.instructions, task.approved_examples)
        try:
            draft = self.gemini.text(system, prompt)
        except GeminiError as exc:
            task.stage = DRAFTING
            msg_id = self.bot.send(
                f"⚠️ не смог написать ответ для {issue_link(task)}: <code>{esc(str(exc)[:300])}</code>",
                keyboard([("🔁 ещё раз", f"redo:{task.key}"), ("отмена", f"drop:{task.key}")]),
            )
            task.message_ids.append(msg_id)
            self.store.put(task)
            return
        self._count_text()
        task.draft = clean_draft(draft)
        task.stage = AWAITING_POST
        self.send_draft_card(task)

    def _count_text(self) -> None:
        author = getattr(self.gemini, "last_author", "")
        if author:
            self.store.bump(f"texts_{author}")

    def _cleanup(self, task: Task) -> None:
        """На сервере клон после закрытия задачи не нужен: diff уже отправлен в Telegram."""
        if not self.cfg.clean_workdirs or not task.workdir:
            return
        path = Path(task.workdir)
        # Удаляем только папки внутри WORK_DIR — на случай испорченного состояния.
        if path.resolve().is_relative_to(self.cfg.work_dir.resolve()) and path.exists():
            shutil.rmtree(path, ignore_errors=True)
            log.info("Удалил рабочую копию %s", path)
        task.workdir = ""

    def send_draft_card(self, task: Task) -> None:
        lines = [
            f"✍️ <b>ответ для {issue_link(task)}</b>",
            f"<blockquote>{esc(task.draft)}</blockquote>",
        ]
        if task.approved_examples and task.draft_mode == "offer":
            who = ", ".join(ex["login"] for ex in task.approved_examples)
            lines.append(f"<i>учёл одобренные отклики в этом репо: {esc(who)}</i>")
        if task.ai_policy:
            lines.append(f"⚠️ <b>в правилах репо есть про ai:</b>\n<i>{esc(task.ai_policy)}</i>")
        lines.append("<i>ответь на это сообщение текстом, чтобы поправить</i>")
        msg_id = self.bot.send(
            "\n\n".join(lines),
            keyboard([("✅ отправить", f"post:{task.key}"), ("🔁 переписать", f"redo:{task.key}")],
                     [("отмена", f"drop:{task.key}")]),
        )
        task.message_ids.append(msg_id)
        self.store.put(task)

    def on_skip(self, key: str) -> None:
        log.info("Пропущено: %s", key)

    def on_post(self, key: str) -> None:
        task = self.store.get(key)
        if not task or task.stage != AWAITING_POST:
            self.bot.send("этот черновик уже неактуален")
            return
        issue = self.gh.issue(key)
        if issue is None or issue["state"] != "OPEN":
            task.stage = DROPPED
            self.store.put(task)
            self.bot.send(f"{issue_link(task)} закрыт или удалён, не отправляю")
            return
        try:
            comment = self.gh.post_comment(task.repo, task.number, task.draft)
        except (AccountMismatch, GhError) as exc:
            self.bot.send(f"⚠️ не отправилось: <code>{esc(str(exc)[:500])}</code>")
            self.send_draft_card(task)
            return
        task.comment_id, task.comment_url = comment["id"], comment["url"]
        task.posted_at = utc_now()
        task.stage = POSTED
        task.draft_mode, task.reply_to_comment, task.instructions = "offer", "", []
        self.store.put(task)
        self.store.bump("posted")
        self.bot.send(f'✅ <a href="{esc(comment["url"])}">ответ опубликован</a> в {issue_link(task)}. '
                      f"слежу за ответом мейнтейнера")

    def on_redo(self, key: str) -> None:
        task = self.store.get(key)
        if not task or task.stage not in (DRAFTING, AWAITING_POST):
            self.bot.send("этот черновик уже неактуален")
            return
        issue = self.gh.issue(key)
        if issue is None:
            self.bot.send("не нашёл этот issue, возможно его удалили")
            return
        self.write_draft(task, issue)

    def on_drop(self, key: str) -> None:
        task = self.store.get(key)
        if task and task.stage in ACTIVE_STAGES:
            task.stage = DROPPED
            self._cleanup(task)
            self.store.put(task)
            note = f"\nрабочая копия осталась в <code>{esc(task.workdir)}</code>" if task.workdir else ""
            self.bot.send(f"отменил {issue_link(task)}{note}")

    def on_instruction(self, task: Task, text: str) -> None:
        if task.stage in (DRAFTING, AWAITING_POST):
            task.instructions.append(text)
            self.store.put(task)
            self.on_redo(task.key)
        elif task.stage == REVIEW:
            task.instructions.append(text)
            task.stage = WORKING
            self.store.put(task)
            issue = self.gh.issue(task.key)
            if issue is None:
                self._drop(task, f"{issue_link(task)} удалён, снимаю задачу")
                return
            self.start_work(task, issue, rework=True)
        else:
            self.bot.send(f"{issue_link(task)} сейчас в статусе {task.stage}, правка не применима")

    # --------------------------------------------------------- watcher

    def watch(self) -> None:
        for task in self.store.by_stage(POSTED):
            try:
                self.check_task(task)
            except (GhError, TelegramError) as exc:
                log.warning("Проверка %s#%d не удалась: %s", task.repo, task.number, exc)

    def _drop(self, task: Task, text: str) -> None:
        task.stage = DROPPED
        self._cleanup(task)
        self.store.put(task)
        self.bot.send(text)

    def check_task(self, task: Task) -> None:
        issue = self.gh.issue(task.key)
        if issue is None or issue["state"] != "OPEN":
            self._drop(task, f"🔒 {issue_link(task)} закрыт, снимаю задачу")
            return
        me = self.cfg.github_user.lower()
        assignees = [a.lower() for a in issue["assignees"]]
        if me in assignees:
            self.on_consent(task, issue, "тебя назначили исполнителем")
            return
        if assignees:
            self._drop(task, f"❌ {issue_link(task)} назначили на {esc(', '.join(issue['assignees']))}, снимаю задачу")
            return

        author = issue["author"].lower()
        fresh = [
            c for c in issue["comments"]
            if c["id"] not in task.reported_comment_ids
            and c["created_at"] > task.posted_at
            and not c["is_bot"]
            and c["login"].lower() != me
            and (c["association"] in MAINTAINER_ROLES or c["login"].lower() == author)
        ]
        for comment in fresh:
            task.reported_comment_ids.append(comment["id"])
            self.store.put(task)
            try:
                result = self.gemini.json(
                    prompts.CONSENT_SYSTEM,
                    prompts.consent_prompt(self.cfg.github_user, task.draft, comment["login"], comment["body"]),
                )
                verdict, summary = result.get("verdict", "other"), str(result.get("summary", ""))
            except GeminiError as exc:
                log.warning("Не удалось классифицировать ответ: %s", exc)
                verdict, summary = "other", "не смог разобрать ответ"
            quote = f"<blockquote>{esc(comment['body'][:1500])}</blockquote>"
            who = f"<b>{esc(comment['login'])}</b> в {issue_link(task)}"

            if verdict == "approve":
                self.on_consent(task, issue, f"{comment['login']}: {summary}")
                return
            if verdict == "reject":
                self._drop(task, f"❌ {who} отказал: {esc(summary)}\n{quote}")
                return
            if verdict == "question":
                self.bot.send(f"❓ {who} спрашивает: {esc(summary)}\n{quote}")
                task.draft_mode, task.reply_to_comment, task.instructions = "reply", comment["body"], []
                self.write_draft(task, issue)
                return
            self.bot.send(f"💬 {who}: {esc(summary)}\n{quote}")

        posted = datetime.fromisoformat(task.posted_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - posted > timedelta(days=WATCH_GIVE_UP_DAYS):
            self._drop(task, f"⌛ по {issue_link(task)} нет ответа {WATCH_GIVE_UP_DAYS} дней, снимаю задачу")

    def on_consent(self, task: Task, issue: dict[str, Any], reason: str) -> None:
        task.stage = WORKING
        self.store.put(task)
        self.bot.send(f"🟢 согласие по {issue_link(task)}: {esc(reason)}\nначинаю работу")
        self.start_work(task, issue)

    # ------------------------------------------------------------ work

    def _offer_retry(self, task: Task, reason: str) -> None:
        msg_id = self.bot.send(
            f"⚠️ {issue_link(task)}: {esc(reason)}",
            keyboard([("🔁 ещё раз", f"work:{task.key}"), ("отказаться", f"drop:{task.key}")]),
        )
        task.message_ids.append(msg_id)
        self.store.put(task)

    def start_work(self, task: Task, issue: dict[str, Any], rework: bool = False) -> None:
        try:
            path = Path(task.workdir) if task.workdir else None
            if not rework or path is None or not path.exists():
                task.branch = task.branch or f"bounty/{task.number}-{workspace.slugify(task.title)}"
                path, task.base_sha = workspace.prepare(self.cfg.work_dir, task.repo, issue["default_branch"], task.branch)
                task.workdir = str(path)
                self.store.put(task)
                rework = False
            current = workspace.diff(path, task.base_sha)[1] if rework else ""
            prompt = coder.task_prompt(issue, task.instructions if rework else [], current)
            result = self.claude.solve(path, prompt)
            workspace.commit_all(path, result["commit_message"], self.cfg.github_user, self.gh.noreply_email())
            stat, patch = workspace.diff(path, task.base_sha)
        except (workspace.GitError, coder.CoderError, GhError) as exc:
            log.warning("Работа над %s#%d не удалась: %s", task.repo, task.number, exc)
            self._offer_retry(task, f"не получилось: {str(exc)[:400]}")
            return
        if not patch.strip():
            self._offer_retry(task, "изменений нет")
            return
        task.summary, task.commit_message = result["summary"], result["commit_message"]
        task.instructions = []
        task.stage = REVIEW
        self.store.put(task)
        self.send_review(task, stat, patch)

    def send_review(self, task: Task, stat: str, patch: str) -> None:
        text = "\n\n".join([
            f"🧩 <b>готово: {issue_link(task)}</b>",
            f"<blockquote>{esc(task.summary)}</blockquote>",
            f"<pre>{esc(stat[-1500:])}</pre>",
            ("" if self.cfg.clean_workdirs else f"📁 <code>{esc(task.workdir)}</code>\n")
            + "код писал claude без запуска команд: прогони тесты сам перед PR (diff ниже)",
            "<i>ответь на это сообщение, чтобы доработать</i>",
        ])
        msg_id = self.bot.send(text, keyboard([("🚀 открыть PR", f"pr:{task.key}"), ("🗑 отказаться", f"drop:{task.key}")]))
        task.message_ids.append(msg_id)
        doc_id = self.bot.send_document(f"{task.repo.replace('/', '__')}-{task.number}.patch", patch.encode("utf-8"),
                                        caption=f"diff для {issue_link(task)}")
        task.message_ids.append(doc_id)
        self.store.put(task)

    def on_work(self, key: str) -> None:
        task = self.store.get(key)
        if not task or task.stage != WORKING:
            self.bot.send("эта задача уже не в работе")
            return
        issue = self.gh.issue(key)
        if issue is None or issue["state"] != "OPEN":
            self._drop(task, f"🔒 {issue_link(task)} закрыт или удалён, снимаю задачу")
            return
        # Если раньше уже был удачный результат — дорабатываем его, а не начинаем заново.
        self.start_work(task, issue, rework=bool(task.summary))

    def on_pr(self, key: str) -> None:
        task = self.store.get(key)
        if not task or task.stage != REVIEW:
            self.bot.send("эта задача не ждёт PR")
            return
        issue = self.gh.issue(key)
        if issue is None or issue["state"] != "OPEN":
            self._drop(task, f"🔒 {issue_link(task)} закрыт или удалён, PR не открываю")
            return
        path = Path(task.workdir)
        if not task.workdir or not (path / ".git").exists():
            # На сервере без диска клон пропадает при перезапуске: код придётся написать заново.
            task.stage = WORKING
            task.summary = ""
            self._offer_retry(task, "рабочая копия пропала после перезапуска сервера, claude напишет код заново")
            return
        try:
            fork = self.gh.fork(task.repo)
            workspace.push(path, fork, task.branch)
            stat, _ = workspace.diff(path, task.base_sha)
            try:
                meta = self.gemini.json(prompts.pr_system(prompts.load_style(self.cfg.style_file)),
                                        prompts.pr_prompt(issue, task.summary, stat))
                self._count_text()
            except GeminiError as exc:
                log.warning("Описание PR не сгенерировано: %s", exc)
                meta = {}
            title = str(meta.get("title") or task.commit_message)
            body = str(meta.get("body") or task.summary).strip() + f"\n\ncloses #{task.number}"
            if prompts.is_algora(issue):
                body += f"\n/claim #{task.number}"
            url = self.gh.create_pr(task.repo, f"{fork.split('/')[0]}:{task.branch}", issue["default_branch"], title, body)
        except (GhError, workspace.GitError) as exc:
            msg_id = self.bot.send(f"⚠️ PR не открылся: <code>{esc(str(exc)[:500])}</code>",
                                   keyboard([("🔁 ещё раз", f"pr:{task.key}"), ("🗑 отказаться", f"drop:{task.key}")]))
            task.message_ids.append(msg_id)
            self.store.put(task)
            return
        task.pr_url = url
        task.stage = PR_OPENED
        self._cleanup(task)
        self.store.put(task)
        self.bot.send(f'🚀 <a href="{esc(url)}">PR открыт</a> для {issue_link(task)}')

    def on_status(self) -> None:
        active = [t for t in self.store.tasks.values() if t.stage in ACTIVE_STAGES]
        if not active:
            self.bot.send("активных задач нет")
            return
        self.bot.send("\n".join(f"• {issue_link(t)} — {t.stage}" for t in active))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    try:
        cfg = AgentConfig.from_env()
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 1
    claude = coder.ClaudeCoder(cfg.claude_bin, cfg.claude_model, cfg.coder_timeout)
    if not claude.available():
        log.error("Не найден %s: установи Claude Code CLI или укажи путь в CLAUDE_BIN", cfg.claude_bin)
        return 1
    if cfg.port:
        # Порт открываем сразу: Render ждёт его, пока агент восстанавливает состояние.
        keepalive.start_server(cfg.port)
        if cfg.public_url:
            keepalive.start_self_ping(cfg.public_url)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    gist: GistState | None = None
    try:
        gh = GitHub(cfg.github_user)
        if cfg.state_gist:
            gist = GistState(gh, cfg.state_file)
            gist.restore()
            gist.start()

            def on_term(signum: int, frame: Any) -> None:
                raise KeyboardInterrupt

            # Render останавливает сервис через SIGTERM: успеваем сохранить состояние.
            signal.signal(signal.SIGTERM, on_term)
        # Тексты: Gemini, а если он недоступен (регион, квота) — Claude без инструментов.
        texts = Writer(Gemini(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_proxy),
                       ClaudeWriter(cfg.claude_bin, cfg.claude_writer_model))
        app = App(cfg, Bot(cfg.tg_bot_token, cfg.tg_chat_id), texts, gh, TaskStore(cfg.state_file), claude)
        app.run_forever()
    except AccountMismatch as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Остановлено")
    finally:
        if gist is not None:
            try:
                gist.push()
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось сохранить состояние в gist при остановке: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
