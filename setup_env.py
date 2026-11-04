#!/usr/bin/env python3
"""Мастер настройки .env для локального агента.

Спрашивает только токен бота и ключ Gemini (ввод скрыт), проверяет их,
сам определяет TG_CHAT_ID и TG_OWNER_ID по сообщению боту и пишет .env.
Секреты на экран не выводятся.
"""

from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"
TELEGRAM_API = "https://api.telegram.org"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.8-flash"
ZERO_QUOTA_RE = re.compile(r"limit:\s*0\b")
WAIT_FOR_MESSAGE = 180
TIMEOUT = 30


class SetupError(Exception):
    pass


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def write_env(path: Path, template: Path, values: dict[str, str]) -> None:
    """Заполняет шаблон значениями, сохраняя комментарии; лишние ключи дописывает в конец."""
    lines = template.read_text(encoding="utf-8").splitlines() if template.exists() else []
    used: set[str] = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in values:
            out.append(f"{key}={values[key]}")
            used.add(key)
        else:
            out.append(line)
    out += [f"{key}={value}" for key, value in values.items() if key not in used]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


def ask_secret(prompt: str, current: str) -> str:
    hint = " (Enter — оставить текущий)" if current else ""
    value = getpass.getpass(f"{prompt}{hint}: ").strip()
    if not value and current:
        return current
    if not value:
        raise SetupError("пустое значение")
    print(f"  принято, {len(value)} символов")
    return value


def telegram(token: str, method: str, **params) -> dict | list:
    try:
        resp = requests.post(f"{TELEGRAM_API}/bot{token}/{method}", json=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SetupError(f"Telegram недоступен: {type(exc).__name__}") from exc
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code == 401:
        raise SetupError("Telegram не принял токен — проверь его в @BotFather")
    if resp.status_code == 409:
        raise SetupError("у бота включён webhook или уже запущен агент — останови агент и повтори")
    if not data.get("ok"):
        raise SetupError(f"Telegram {method}: {data.get('description', resp.status_code)}")
    return data["result"]


def detect_chat(token: str, bot_username: str, wait: int = WAIT_FOR_MESSAGE) -> tuple[int, int, str]:
    """Ждёт новое сообщение боту; возвращает (chat_id, user_id, название чата)."""
    # Старые апдейты пропускаем: нужен именно ответ, отправленный сейчас.
    pending = telegram(token, "getUpdates", timeout=0)
    offset = pending[-1]["update_id"] + 1 if pending else None
    print(f"\nнапиши боту @{bot_username} любое сообщение: https://t.me/{bot_username}")
    print("(для группы — добавь бота в группу и напиши там)")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        params = {"timeout": 20, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for update in telegram(token, "getUpdates", **params):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat, sender = message.get("chat") or {}, message.get("from") or {}
            if chat.get("id") and sender.get("id") and not sender.get("is_bot"):
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or "чат"
                # Подтверждаем апдейт, чтобы агент не получил его как команду.
                telegram(token, "getUpdates", offset=offset, timeout=0)
                return chat["id"], sender["id"], title
    raise SetupError(f"за {wait} с сообщение не пришло")


def check_gemini(key: str, model: str) -> None:
    try:
        resp = requests.get(f"{GEMINI_API}/{model}", headers={"x-goog-api-key": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SetupError(f"Gemini недоступен: {type(exc).__name__}") from exc
    if resp.status_code in (400, 401, 403):
        raise SetupError("Gemini не принял ключ — создай новый на https://aistudio.google.com/apikey")
    if resp.status_code == 404:
        raise SetupError(f"модель {model} недоступна для этого ключа")
    if resp.status_code != 200:
        raise SetupError(f"Gemini: HTTP {resp.status_code}")

    # Модель видна и на бесплатном тарифе, даже если квоты на неё нет — проверяем пробным запросом.
    try:
        resp = requests.post(
            f"{GEMINI_API}/{model}:generateContent", headers={"x-goog-api-key": key},
            json={"contents": [{"role": "user", "parts": [{"text": "reply with: ok"}]}]}, timeout=120,
        )
    except requests.RequestException as exc:
        raise SetupError(f"Gemini недоступен: {type(exc).__name__}") from exc
    if resp.status_code == 429 and ZERO_QUOTA_RE.search(resp.text):
        raise SetupError(f"у ключа нет квоты на {model} (на бесплатном тарифе она 0): подключи оплату "
                         f"в aistudio.google.com или впиши в .env GEMINI_MODEL=gemini-3.8-flash")
    if resp.status_code == 503:
        print(f"  ⚠ {model} сейчас перегружена (503), ключ принят — агент повторит запрос сам")
        return
    if resp.status_code != 200:
        raise SetupError(f"Gemini generateContent: HTTP {resp.status_code}")


def gh_user() -> str:
    if not shutil.which("gh"):
        return ""
    proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    values = read_env(ENV_FILE)
    values.setdefault("GEMINI_MODEL", DEFAULT_MODEL)
    values["GEMINI_MODEL"] = values["GEMINI_MODEL"] or DEFAULT_MODEL
    values["GITHUB_USER"] = values.get("GITHUB_USER") or "prodbykwiz-hub"
    try:
        print("== Telegram ==")
        token = ask_secret("токен бота от @BotFather", values.get("TG_BOT_TOKEN", ""))
        bot = telegram(token, "getMe")
        print(f"  бот: @{bot['username']}")
        chat_id, owner_id, title = detect_chat(token, bot["username"])
        print(f"  чат: {title} (id {chat_id}), твой id: {owner_id}")
        telegram(token, "sendMessage", chat_id=chat_id, text="✅ бот подключён к агенту bounty hunter")
        values.update(TG_BOT_TOKEN=token, TG_CHAT_ID=str(chat_id), TG_OWNER_ID=str(owner_id))

        print("\n== Gemini ==")
        key = ask_secret("ключ с https://aistudio.google.com/apikey", values.get("GEMINI_API_KEY", ""))
        check_gemini(key, values["GEMINI_MODEL"])
        print(f"  ключ работает, модель {values['GEMINI_MODEL']} доступна")
        values["GEMINI_API_KEY"] = key
    except SetupError as exc:
        print(f"\n✗ {exc}\n.env не изменён")
        return 1
    except KeyboardInterrupt:
        print("\nотменено, .env не изменён")
        return 1

    write_env(ENV_FILE, TEMPLATE, values)
    print(f"\n✓ записано в {ENV_FILE}")

    user = gh_user()
    if user.lower() == values["GITHUB_USER"].lower():
        print(f"✓ gh залогинен как {user}")
    else:
        print(f"⚠ gh залогинен как {user or 'никто'}, а нужен {values['GITHUB_USER']} — "
              f"без этого агент не запустится (см. README)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
