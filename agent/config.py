"""Конфигурация агента из окружения и файла .env (без сторонних зависимостей)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Подхватывает KEY=VALUE из .env; уже заданные переменные окружения не перетирает."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "TG_BOT_TOKEN", "GEMINI_API_KEY")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должно быть >= {minimum}")
    return value


@dataclass(frozen=True)
class AgentConfig:
    tg_bot_token: str
    tg_chat_id: str
    # Нажатия и ответы принимаются только от этого пользователя Telegram.
    tg_owner_id: int
    gemini_api_key: str
    gemini_model: str
    github_user: str
    work_dir: Path
    state_file: Path
    style_file: Path
    watch_interval: int
    # Код пишет Claude Code CLI; Gemini — только тексты сообщений.
    claude_bin: str = "claude"
    claude_model: str = ""
    coder_timeout: int = 1800
    # Репозиторий сканера: агент сам запускает его workflow, потому что cron GitHub ненадёжен.
    # Пустое значение (SCAN_REPO=none) — не запускать.
    scan_repo: str = ""
    scan_workflow: str = "bounty_hunter.yml"
    scan_interval: int = 900
    # Gemini через прокси (http://host:port); Claude пишет тексты, если Gemini недоступен.
    gemini_proxy: str = ""
    claude_writer_model: str = "sonnet"
    # Час (UTC) ежедневного отчёта в Telegram; None — не слать.
    report_hour: int | None = 6
    # Удалять клон репозитория, когда задача закрыта (PR открыт или отменена) — для сервера.
    clean_workdirs: bool = False
    # Бесплатный веб-сервис Render: HTTP-порт, публичный адрес для самопинга, состояние в gist.
    port: int = 0
    public_url: str = ""
    state_gist: bool = False
    # Автоматический режим: агент сам берёт задачи из очереди сканера, отправляет отклик
    # и открывает PR. В Telegram приходят только уведомления.
    auto_mode: bool = False
    auto_max_offers_per_day: int = 3
    auto_max_active: int = 5
    # Факты о себе для откликов на заказы Freelancer/Superteam.
    gig_profile_file: Path = PROJECT_ROOT / "agent" / "gig_profile.md"

    @classmethod
    def from_env(cls) -> AgentConfig:
        load_dotenv(PROJECT_ROOT / ".env")
        # gh и claude читают токены из окружения напрямую. Перевод строки в конце
        # (его добавляет `clip` при копировании) ломает заголовок Authorization.
        for name in SECRET_ENV:
            value = os.environ.get(name)
            if value is not None and value != value.strip():
                os.environ[name] = value.strip()
        required = {
            "TG_BOT_TOKEN": _env("TG_BOT_TOKEN"),
            "TG_CHAT_ID": _env("TG_CHAT_ID"),
            "TG_OWNER_ID": _env("TG_OWNER_ID"),
            "GEMINI_API_KEY": _env("GEMINI_API_KEY"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(f"Не заданы переменные (.env): {', '.join(missing)}")
        try:
            owner_id = int(required["TG_OWNER_ID"])
        except ValueError as exc:
            raise ConfigError("TG_OWNER_ID должен быть числовым id пользователя Telegram") from exc

        github_user = _env("GITHUB_USER", "prodbykwiz-hub")
        scan_repo = _env("SCAN_REPO", f"{github_user}/bounty-hunter")
        scan_repo = "" if scan_repo.lower() == "none" else scan_repo
        report_raw = _env("REPORT_HOUR", "6")
        if report_raw.lower() == "none":
            report_hour = None
        else:
            report_hour = _env_int("REPORT_HOUR", 6, minimum=0)
            if report_hour > 23:
                raise ConfigError("REPORT_HOUR должно быть от 0 до 23 (час по UTC) или none")

        return cls(
            tg_bot_token=required["TG_BOT_TOKEN"],
            tg_chat_id=required["TG_CHAT_ID"],
            tg_owner_id=owner_id,
            gemini_api_key=required["GEMINI_API_KEY"],
            # Pro (gemini-3.1-pro-preview) доступна только с оплатой: на бесплатном тарифе квота 0.
            gemini_model=_env("GEMINI_MODEL", "gemini-3.8-flash"),
            github_user=github_user,
            work_dir=Path(_env("WORK_DIR", str(Path.home() / "bounty-work"))).expanduser(),
            state_file=Path(_env("AGENT_STATE_FILE", str(PROJECT_ROOT / "agent_state.json"))),
            style_file=Path(_env("STYLE_FILE", str(PROJECT_ROOT / "agent" / "style.md"))),
            watch_interval=_env_int("WATCH_INTERVAL", 300, minimum=60),
            claude_bin=_env("CLAUDE_BIN", "claude"),
            claude_model=_env("CLAUDE_MODEL"),
            coder_timeout=_env_int("CODER_TIMEOUT", 1800, minimum=60),
            scan_repo=scan_repo,
            scan_workflow=_env("SCAN_WORKFLOW", "bounty_hunter.yml"),
            scan_interval=_env_int("SCAN_INTERVAL", 900, minimum=300),
            gemini_proxy=_env("GEMINI_PROXY"),
            claude_writer_model=_env("CLAUDE_WRITER_MODEL", "sonnet"),
            report_hour=report_hour,
            clean_workdirs=_env("CLEAN_WORKDIRS").lower() in ("1", "true", "yes"),
            port=_env_int("PORT", 0, minimum=0),
            # Render сам задаёт RENDER_EXTERNAL_URL веб-сервису.
            public_url=_env("PUBLIC_URL", _env("RENDER_EXTERNAL_URL")),
            state_gist=_env("STATE_GIST").lower() in ("1", "true", "yes"),
            auto_mode=_env("AUTO_MODE").lower() in ("1", "true", "yes"),
            auto_max_offers_per_day=_env_int("AUTO_MAX_OFFERS_PER_DAY", 3, minimum=1),
            auto_max_active=_env_int("AUTO_MAX_ACTIVE", 5, minimum=1),
            gig_profile_file=Path(_env("GIG_PROFILE_FILE", str(PROJECT_ROOT / "agent" / "gig_profile.md"))),
        )
