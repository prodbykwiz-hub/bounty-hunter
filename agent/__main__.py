"""Точка входа: python -m agent"""

from __future__ import annotations

import html
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent import prompts
from agent.config import AgentConfig, ConfigError
from agent.gemini import Gemini, GeminiError
from agent.github import AccountMismatch, GhError, GitHub
from agent.state import (
    ACTIVE_STAGES, AWAITING_POST, DRAFTING, DROPPED, POSTED, WORKING, Task, TaskStore, utc_now,
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
    def __init__(self, cfg: AgentConfig, bot: Bot, gemini: Gemini, gh: GitHub, store: TaskStore) -> None:
        self.cfg = cfg
        self.bot = bot
        self.gemini = gemini
        self.gh = gh
        self.store = store
        self.callbacks: dict[str, Callable[[str], None]] = {
            "take": self.on_take,
            "skip": self.on_skip,
            "post": self.on_post,
            "redo": self.on_redo,
            "drop": self.on_drop,
        }

    # ------------------------------------------------------------ loop

    def run_forever(self) -> None:
        self.gh.ensure_account()
        log.info("Агент запущен: gh=%s, модель=%s", self.cfg.github_user, self.cfg.gemini_model)
        next_watch = 0.0
        while True:
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

    def notify_error(self, exc: Exception) -> None:
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
        self.store.put(task)
        self.write_draft(task, issue)

    def write_draft(self, task: Task, issue: dict[str, Any]) -> None:
        style = prompts.load_style(self.cfg.style_file)
        if task.draft_mode == "reply":
            system = prompts.reply_system(style)
            prompt = prompts.reply_prompt(issue, task.reply_to_comment, task.instructions)
        else:
            system, prompt = prompts.comment_system(style), prompts.comment_prompt(issue, task.instructions)
        try:
            draft = self.gemini.text(system, prompt)
        except GeminiError as exc:
            task.stage = DRAFTING
            msg_id = self.bot.send(
                f"⚠️ gemini не смог написать ответ для {issue_link(task)}: <code>{esc(str(exc)[:300])}</code>",
                keyboard([("🔁 ещё раз", f"redo:{task.key}"), ("отмена", f"drop:{task.key}")]),
            )
            task.message_ids.append(msg_id)
            self.store.put(task)
            return
        task.draft = clean_draft(draft)
        task.stage = AWAITING_POST
        self.send_draft_card(task)

    def send_draft_card(self, task: Task) -> None:
        lines = [
            f"✍️ <b>ответ для {issue_link(task)}</b>",
            f"<blockquote>{esc(task.draft)}</blockquote>",
        ]
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
            self.store.put(task)
            self.bot.send(f"отменил {issue_link(task)}")

    def on_instruction(self, task: Task, text: str) -> None:
        if task.stage in (DRAFTING, AWAITING_POST):
            task.instructions.append(text)
            self.store.put(task)
            self.on_redo(task.key)
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
        self.bot.send(f"🟢 согласие по {issue_link(task)}: {esc(reason)}")

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
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    try:
        gh = GitHub(cfg.github_user)
        app = App(cfg, Bot(cfg.tg_bot_token, cfg.tg_chat_id), Gemini(cfg.gemini_api_key, cfg.gemini_model),
                  gh, TaskStore(cfg.state_file))
        app.run_forever()
    except AccountMismatch as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("Остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
