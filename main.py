#!/usr/bin/env python3
"""GitHub Bounty Hunter.

Ищет открытые оплачиваемые issues (bounties) на GitHub, отсеивает спам
и шлёт уведомления в Telegram. Рассчитан на запуск по расписанию
в GitHub Actions; состояние (уже отправленные задачи) хранится
в seen_issues.json, который workflow коммитит обратно в репозиторий.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

import requests

log = logging.getLogger("bounty_hunter")

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
TELEGRAM_API = "https://api.telegram.org"

REQUEST_TIMEOUT = 20
MAX_RETRIES = 4
# Если GitHub просит ждать дольше — не висим, а доделываем прогон с тем, что есть.
MAX_RATE_LIMIT_SLEEP = 90
# Основной лимит Search API — 30 запросов/мин, но secondary rate limit срабатывает
# гораздо раньше на сериях однотипных поисков, поэтому держим паузу побольше.
SEARCH_MIN_INTERVAL = 6.0
SEARCH_PER_PAGE = 100
SEARCH_MAX_PAGES = 2
# Свежие issues ищем в каждом запуске, остальную часть окна MAX_AGE_HOURS — срезами по очереди.
FRESH_WINDOW_HOURS = 24
BACKFILL_SLICE_HOURS = 72
CRON_PERIOD_SECONDS = 15 * 60
MAX_PER_REPO_PER_RUN = 3
GRAPHQL_REPO_CHUNK = 50
GRAPHQL_COMMENTS_CHUNK = 20
TELEGRAM_MIN_INTERVAL = 1.1
TELEGRAM_MAX_TEXT = 4096
STATE_VERSION = 1

# Язык в запрос не добавляем (каждый язык удваивал бы число поисков) —
# фильтруем по primaryLanguage репозитория из GraphQL.
# GitHub Search игнорирует символ "$", поэтому сумму в заголовке ищем
# через "bounty in:title", а саму сумму достаём регуляркой.
SEARCH_TERMS: tuple[str, ...] = (
    'label:bounty,"💎 Bounty"',
    "algora OR opire",
    "bounty in:title",
)
BOUNTY_PLATFORMS_RE = re.compile(r"\b(?:algora|opire)\b", re.IGNORECASE)
TITLE_BOUNTY_RE = re.compile(r"\$\s?\d.*\bbounty\b|\bbounty\b.*\$\s?\d", re.IGNORECASE)

# ---------------------------------------------------------------- anti-spam

STOP_WORDS: tuple[str, ...] = (
    "bounty seguro",
    "escrow",
    "trauma-station",
    "airdrop",
    "seed phrase",
    "recovery phrase",
    "connect your wallet",
    "claim your reward",
    "free crypto",
)


def _stop_word_pattern(words: Iterable[str]) -> re.Pattern[str]:
    # "trauma-station" ловит и "Trauma Station", и "trauma_station".
    alternatives = [
        r"[\s\-_]*".join(re.escape(token) for token in re.split(r"[\s\-_]+", word))
        for word in words
    ]
    return re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(alternatives) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


STOP_WORDS_RE = _stop_word_pattern(STOP_WORDS)
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# Репозиторий с иероглифами в названии/описании/заголовке должен быть заметно
# популярнее порога, иначе это типичный спам-шаблон.
CJK_MIN_STARS = 100

URL_RE = re.compile(r"https?://[^\s<>()\"'`\]\[]+", re.IGNORECASE)
TRUSTED_DOMAINS: tuple[str, ...] = (
    "github.com",
    "githubusercontent.com",
    "github.io",
    "github.blog",
    "githubstatus.com",
    "githubassets.com",
    "algora.io",
    "opire.dev",
    "gitcoin.co",
    "issuehunt.io",
    "polar.sh",
    "bountysource.com",
)
URL_SHORTENERS = frozenset(
    {
        "bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "is.gd", "rb.gy",
        "shorturl.at", "ow.ly", "rebrand.ly", "tiny.cc", "s.id", "v.gd",
    }
)
SUSPICIOUS_TLDS = frozenset(
    {"xyz", "top", "click", "icu", "buzz", "rest", "cfd", "sbs", "lol", "monster", "quest", "zip", "mov"}
)
# Ключевые слова-приманки в домене: github-reward-claim.com, bounty-payout.net и т.п.
LURE_HOST_WORDS: tuple[str, ...] = ("github", "bounty", "reward", "claim", "airdrop", "wallet", "payout", "escrow")
IP_HOST_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

COMPETITION_RE = re.compile(
    r"/attempt\b"
    r"|\bassign\s+(?:it\s+|this\s+)?(?:to\s+)?me\b"
    r"|\bworking\s+on\s+(?:this|it)\b",
    re.IGNORECASE,
)

# ------------------------------------------------------------------ rewards

_NUM = r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
REWARD_PREFIX_RE = re.compile(r"(?:US)?\$\s?" + _NUM + r"(?:\s?(?P<k>[kK])\b)?")
REWARD_SUFFIX_RE = re.compile(_NUM + r"\s?(?P<k>[kK])?\s?(?:USDC|USDT|USD|\$)(?![A-Za-z])")
MAX_REASONABLE_REWARD = 1_000_000
# Блоки кода выкидываем: иначе `echo $1` превращается в «награду $1».
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def strip_code(text: str) -> str:
    return CODE_RE.sub(" ", text)


def extract_reward(*sources: str) -> float | None:
    """Первая правдоподобная сумма в $ по порядку источников (метки → заголовок → тело → боты)."""
    for text in sources:
        if not text:
            continue
        matches = sorted(
            [*REWARD_PREFIX_RE.finditer(text), *REWARD_SUFFIX_RE.finditer(text)],
            key=lambda m: m.start(),
        )
        for match in matches:
            amount = float(match.group("num").replace(",", ""))
            if match.group("k"):
                amount *= 1000
            if 0 < amount <= MAX_REASONABLE_REWARD:
                return amount
    return None


def format_reward(amount: float | None) -> str:
    if amount is None:
        return "не указана"
    if amount.is_integer():
        return f"${int(amount):,}"
    return f"${amount:,.2f}"


# --------------------------------------------------------------------- time


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def humanize_age(created: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - created).total_seconds()))
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')} назад"
    days = hours // 24
    return f"{days} {_plural(days, 'день', 'дня', 'дней')} назад"


def parse_github_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------- config


class ConfigError(Exception):
    pass


def _env_str(name: str, default: str = "") -> str:
    # Пустой секрет в Actions приходит как пустая строка, а не как отсутствие переменной.
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должно быть >= {minimum}, получено {value}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    github_token: str
    tg_bot_token: str
    tg_chat_id: str
    languages: tuple[str, ...]
    min_stars: int
    min_forks: int
    min_reward: int
    max_age_hours: int
    competition_threshold: int
    skip_high_competition: bool
    max_messages_per_run: int
    state_ttl_days: int
    state_file: Path
    dry_run: bool

    @classmethod
    def from_env(cls) -> Config:
        dry_run = _env_bool("DRY_RUN")
        github_token = _env_str("GITHUB_TOKEN")
        tg_bot_token = _env_str("TG_BOT_TOKEN")
        tg_chat_id = _env_str("TG_CHAT_ID")

        missing = [name for name, value in (("GITHUB_TOKEN", github_token),) if not value]
        if not dry_run:
            missing += [
                name for name, value in (("TG_BOT_TOKEN", tg_bot_token), ("TG_CHAT_ID", tg_chat_id)) if not value
            ]
        if missing:
            raise ConfigError(f"Не заданы переменные окружения: {', '.join(missing)}")

        languages = tuple(lang.strip() for lang in _env_str("LANGUAGES", "C++,Python,Rust,Go").split(",") if lang.strip())
        if not languages:
            raise ConfigError("LANGUAGES не может быть пустым")

        max_age_hours = _env_int("MAX_AGE_HOURS", 720, minimum=1)
        # TTL записи обязан пережить окно поиска, иначе задача найдётся снова и придёт дубль.
        min_ttl_days = max_age_hours // 24 + 2
        state_ttl_days = max(_env_int("STATE_TTL_DAYS", 14, minimum=1), min_ttl_days)

        return cls(
            github_token=github_token,
            tg_bot_token=tg_bot_token,
            tg_chat_id=tg_chat_id,
            languages=languages,
            min_stars=_env_int("MIN_STARS", 30),
            min_forks=_env_int("MIN_FORKS", 5),
            min_reward=_env_int("MIN_REWARD", 25),
            max_age_hours=max_age_hours,
            competition_threshold=_env_int("COMPETITION_THRESHOLD", 3),
            skip_high_competition=_env_bool("SKIP_HIGH_COMPETITION"),
            max_messages_per_run=_env_int("MAX_MESSAGES_PER_RUN", 20, minimum=1),
            state_ttl_days=state_ttl_days,
            state_file=Path(_env_str("STATE_FILE", "seen_issues.json")),
            dry_run=dry_run,
        )


# -------------------------------------------------------------------- state


class SeenStore:
    """Хранилище уже обработанных issues: {node_id: {"at", "status", "url"}}."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.issues: dict[str, dict[str, str]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Файл состояния %s не найден — начинаем с пустого", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            issues = data.get("issues", {}) if isinstance(data, dict) else {}
            if not isinstance(issues, dict):
                raise ValueError("поле 'issues' должно быть объектом")
            self.issues = issues
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            log.error("Файл состояния повреждён (%s), сохраняю копию в %s", exc, backup)
            try:
                self.path.replace(backup)
            except OSError:
                log.exception("Не удалось сохранить копию повреждённого файла")
            self.issues = {}
            self._dirty = True
        log.info("Загружено %d записей о просмотренных issues", len(self.issues))

    def __contains__(self, key: str) -> bool:
        return key in self.issues

    def add(self, key: str, status: str, url: str) -> None:
        self.issues[key] = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "url": url,
        }
        self._dirty = True

    def prune(self, ttl_days: int) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        stale = []
        for key, entry in self.issues.items():
            try:
                if parse_github_time(entry["at"]) < cutoff:
                    stale.append(key)
            except (KeyError, TypeError, ValueError):
                stale.append(key)
        for key in stale:
            del self.issues[key]
        if stale:
            self._dirty = True
            log.info("Удалено %d устаревших записей из состояния", len(stale))

    def save(self) -> None:
        if not self._dirty:
            log.info("Состояние не изменилось, файл не перезаписываю")
            return
        payload = {"version": STATE_VERSION, "issues": dict(sorted(self.issues.items()))}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
        log.info("Состояние сохранено: %d записей", len(self.issues))


# ------------------------------------------------------------------- models


@dataclass
class RepoInfo:
    full_name: str
    stars: int
    forks: int
    language: str | None
    description: str
    archived: bool


@dataclass
class Candidate:
    key: str
    number: int
    title: str
    body: str
    url: str
    repo: str
    author: str
    labels: list[str]
    comments_count: int
    created_at: datetime
    author_is_bot: bool = False
    repo_info: RepoInfo | None = None
    competitors: int = 0
    reward: float | None = None
    bot_comments: list[str] = field(default_factory=list)

    @classmethod
    def from_search_item(cls, item: dict[str, Any]) -> Candidate:
        repo_parts = item["repository_url"].rstrip("/").split("/")
        user = item.get("user") or {}
        return cls(
            key=item["node_id"],
            number=item["number"],
            title=item.get("title") or "",
            body=item.get("body") or "",
            url=item["html_url"],
            repo=f"{repo_parts[-2]}/{repo_parts[-1]}",
            author=user.get("login") or "",
            labels=[label.get("name", "") for label in item.get("labels") or []],
            comments_count=int(item.get("comments") or 0),
            created_at=parse_github_time(item["created_at"]),
            author_is_bot=user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]"),
        )


# ------------------------------------------------------------------ helpers


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def is_suspicious_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host:
        return False
    host = host.removeprefix("www.")
    if any(host == domain or host.endswith("." + domain) for domain in TRUSTED_DOMAINS):
        return False
    if IP_HOST_RE.match(host) or host in URL_SHORTENERS:
        return True
    if host.rsplit(".", 1)[-1] in SUSPICIOUS_TLDS:
        return True
    return any(word in host for word in LURE_HOST_WORDS)


def content_spam_reason(candidate: Candidate) -> str | None:
    """Проверки, не требующие запросов к API (стоп-слова, автор, фишинговые ссылки)."""
    # Настоящие баунти заводят люди; issues от ботов — это новостные ленты и авто-intake.
    if candidate.author_is_bot:
        return f"issue создан ботом {candidate.author}"
    for source_name, text in (("заголовке", candidate.title), ("теле", candidate.body)):
        match = STOP_WORDS_RE.search(text)
        if match:
            return f"стоп-слово {match.group(0)!r} в {source_name}"

    normalized_author = re.sub(r"[^a-z0-9]", "", candidate.author.lower())
    for word in STOP_WORDS:
        if re.sub(r"[^a-z0-9]", "", word) in normalized_author:
            return f"стоп-слово {word!r} в имени автора"

    for url in URL_RE.findall(candidate.body):
        if is_suspicious_url(url):
            return f"подозрительная ссылка {url[:80]}"
    return None


def repo_spam_reason(candidate: Candidate, info: RepoInfo | None, cfg: Config) -> str | None:
    if info is None:
        return "репозиторий недоступен"
    if info.archived:
        return "репозиторий в архиве"
    if info.stars < cfg.min_stars:
        return f"мало звёзд ({info.stars} < {cfg.min_stars})"
    if info.forks < cfg.min_forks:
        return f"мало форков ({info.forks} < {cfg.min_forks})"
    wanted = {lang.lower() for lang in cfg.languages}
    if (info.language or "").lower() not in wanted:
        return f"язык репозитория {info.language or 'не определён'}"
    cjk_text = f"{info.full_name} {info.description} {candidate.title}"
    if CJK_RE.search(cjk_text) and info.stars < CJK_MIN_STARS:
        return f"иероглифы в репозитории с {info.stars} звёздами"
    return None


def has_bounty_signal(candidate: Candidate) -> bool:
    """Сумма засчитывается, только если рядом есть признак настоящего баунти."""
    if any("bounty" in label.lower() for label in candidate.labels):
        return True
    if candidate.bot_comments and any(BOUNTY_PLATFORMS_RE.search(body) for body in candidate.bot_comments):
        return True
    return bool(BOUNTY_PLATFORMS_RE.search(candidate.body) or TITLE_BOUNTY_RE.search(candidate.title))


# ------------------------------------------------------------------- GitHub


class RateLimitError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bounty-hunter-bot",
            }
        )
        self._last_search_at = 0.0

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> int | None:
        """Сколько секунд ждать, если ответ — это rate limit; None, если это другая ошибка."""
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return int(retry_after) + 1
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0") or 0)
            return max(1, reset - int(time.time()) + 1)
        if "rate limit" in resp.text.lower():
            # Secondary rate limit без заголовков: документация советует ждать минуту.
            return 60
        return None

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                wait = 2**attempt
                log.warning("GitHub: сетевая ошибка %s (попытка %d/%d), жду %d с",
                            type(exc).__name__, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue

            if resp.status_code in (403, 429):
                wait = self._rate_limit_wait(resp)
                if wait is not None:
                    if wait > MAX_RATE_LIMIT_SLEEP:
                        raise RateLimitError(f"GitHub rate limit, сброс через {wait} с")
                    log.warning("GitHub rate limit, жду %d с (попытка %d/%d)", wait, attempt, MAX_RETRIES)
                    time.sleep(wait)
                    continue

            if resp.status_code >= 500:
                wait = 2**attempt
                log.warning("GitHub: HTTP %d (попытка %d/%d), жду %d с",
                            resp.status_code, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue

            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                log.debug("GitHub %s: осталось %s запросов (%s)",
                          resp.headers.get("X-RateLimit-Resource", "?"), remaining, url)
            return resp
        raise RuntimeError(f"GitHub: {method} {url} не удался после {MAX_RETRIES} попыток")

    def _throttle_search(self) -> None:
        elapsed = time.monotonic() - self._last_search_at
        if elapsed < SEARCH_MIN_INTERVAL:
            time.sleep(SEARCH_MIN_INTERVAL - elapsed)
        self._last_search_at = time.monotonic()

    def search_issues(self, query: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, SEARCH_MAX_PAGES + 1):
            self._throttle_search()
            resp = self._request(
                "GET",
                f"{GITHUB_API}/search/issues",
                params={"q": query, "sort": "created", "order": "desc", "per_page": SEARCH_PER_PAGE, "page": page},
            )
            if resp.status_code == 422:
                log.warning("Поиск отклонён GitHub (422) для запроса %r: %s", query, resp.text[:200])
                break
            resp.raise_for_status()
            data = resp.json()
            if data.get("incomplete_results"):
                log.info("GitHub вернул неполные результаты для %r", query)
            batch = data.get("items") or []
            items.extend(batch)
            total = min(int(data.get("total_count") or 0), 1000)
            if len(batch) < SEARCH_PER_PAGE or page * SEARCH_PER_PAGE >= total:
                break
        return items

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", GITHUB_GRAPHQL, json={"query": query, "variables": variables})
        resp.raise_for_status()
        payload = resp.json()
        errors = payload.get("errors") or []
        if any(err.get("type") == "RATE_LIMITED" for err in errors):
            raise RateLimitError("GraphQL rate limit")
        for err in errors:
            # NOT_FOUND для удалённых/приватных репозиториев — нормально, данные частичные.
            log.debug("GraphQL: %s", err.get("message"))
        rate = (payload.get("data") or {}).get("rateLimit") or {}
        if rate:
            log.debug("GraphQL: стоимость %s, осталось %s", rate.get("cost"), rate.get("remaining"))
        return payload.get("data") or {}

    def fetch_repos(self, full_names: list[str]) -> dict[str, RepoInfo]:
        result: dict[str, RepoInfo] = {}
        for chunk in chunked(full_names, GRAPHQL_REPO_CHUNK):
            var_defs, parts, variables = [], [], {}
            for i, full_name in enumerate(chunk):
                owner, name = full_name.split("/", 1)
                var_defs += [f"$o{i}: String!", f"$n{i}: String!"]
                variables[f"o{i}"], variables[f"n{i}"] = owner, name
                parts.append(
                    f"r{i}: repository(owner: $o{i}, name: $n{i}) {{"
                    " nameWithOwner stargazerCount forkCount isArchived description"
                    " primaryLanguage { name } }"
                )
            query = f"query({', '.join(var_defs)}) {{ {' '.join(parts)} rateLimit {{ cost remaining }} }}"
            data = self.graphql(query, variables)
            for i, full_name in enumerate(chunk):
                node = data.get(f"r{i}")
                if not node:
                    continue
                result[full_name] = RepoInfo(
                    full_name=node["nameWithOwner"],
                    stars=node["stargazerCount"],
                    forks=node["forkCount"],
                    language=(node.get("primaryLanguage") or {}).get("name"),
                    description=node.get("description") or "",
                    archived=bool(node.get("isArchived")),
                )
        return result

    def fetch_comments(self, candidates: list[Candidate]) -> dict[str, dict[str, Any]]:
        """{candidate.key: {"total": int, "comments": [(login, is_bot, body), ...]}}"""
        result: dict[str, dict[str, Any]] = {}
        for chunk in chunked(candidates, GRAPHQL_COMMENTS_CHUNK):
            var_defs, parts, variables = [], [], {}
            for i, cand in enumerate(chunk):
                owner, name = cand.repo.split("/", 1)
                var_defs += [f"$o{i}: String!", f"$n{i}: String!", f"$i{i}: Int!"]
                variables[f"o{i}"], variables[f"n{i}"], variables[f"i{i}"] = owner, name, cand.number
                parts.append(
                    f"c{i}: repository(owner: $o{i}, name: $n{i}) {{ issue(number: $i{i}) {{"
                    " comments(last: 100) { totalCount nodes { body author { __typename login } } } } }"
                )
            query = f"query({', '.join(var_defs)}) {{ {' '.join(parts)} rateLimit {{ cost remaining }} }}"
            data = self.graphql(query, variables)
            for i, cand in enumerate(chunk):
                comments = (((data.get(f"c{i}") or {}).get("issue") or {}).get("comments")) or None
                if comments is None:
                    continue
                result[cand.key] = {
                    "total": comments.get("totalCount", 0),
                    "comments": [
                        (
                            (node.get("author") or {}).get("login") or "",
                            (node.get("author") or {}).get("__typename") == "Bot",
                            node.get("body") or "",
                        )
                        for node in comments.get("nodes") or []
                    ],
                }
        return result


# ----------------------------------------------------------------- Telegram


class TelegramClient:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()
        self._last_sent_at = 0.0

    def send(self, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < TELEGRAM_MIN_INTERVAL:
            time.sleep(TELEGRAM_MIN_INTERVAL - elapsed)

        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                except requests.RequestException as exc:
                    wait = 2**attempt
                    log.warning("Telegram: сетевая ошибка %s (попытка %d/%d): %s", type(exc).__name__,
                                attempt, MAX_RETRIES, _redact(str(exc), self.token))
                    time.sleep(wait)
                    continue

                try:
                    data = resp.json()
                except ValueError:
                    data = {}

                if resp.status_code == 429:
                    wait = int((data.get("parameters") or {}).get("retry_after") or 5) + 1
                    log.warning("Telegram flood control, жду %d с", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    time.sleep(2**attempt)
                    continue
                if not data.get("ok"):
                    log.error("Telegram отклонил сообщение (HTTP %d): %s",
                              resp.status_code, data.get("description", resp.text[:200]))
                    return False
                return True
            log.error("Telegram: сообщение не отправлено после %d попыток", MAX_RETRIES)
            return False
        finally:
            self._last_sent_at = time.monotonic()


def format_message(cand: Candidate, cfg: Config, now: datetime | None = None) -> str:
    info = cand.repo_info
    esc = html.escape
    title = cand.title if len(cand.title) <= 200 else cand.title[:197] + "…"
    stars = f"{info.stars:,}" if info else "?"
    language = (info.language if info else None) or "?"

    lines = [
        f'🎯 <b><a href="{esc(cand.url)}">{esc(title)}</a></b>',
        f'📦 <a href="https://github.com/{esc(cand.repo)}">{esc(cand.repo)}</a> · ⭐️ {stars} · {esc(language)}',
        f"💰 Награда: <b>{esc(format_reward(cand.reward))}</b>",
        f"💬 Комментариев: {cand.comments_count}",
        f"🕒 Опубликовано {humanize_age(cand.created_at, now)}",
    ]
    if cand.competitors > cfg.competition_threshold:
        lines.append(f"⚠️ <b>Высокая конкуренция</b>: {cand.competitors} претендентов")
    return "\n".join(lines)[:TELEGRAM_MAX_TEXT]


def card_keyboard(cand: Candidate) -> dict[str, Any] | None:
    """Кнопки карточки; нажатия обрабатывает локальный агент (python -m agent)."""
    take, skip = f"take:{cand.key}", f"skip:{cand.key}"
    # Telegram ограничивает callback_data 64 байтами.
    if len(take.encode()) > 64:
        return None
    return {"inline_keyboard": [[
        {"text": "✍️ взять", "callback_data": take},
        {"text": "пропустить", "callback_data": skip},
    ]]}


# ----------------------------------------------------------------- pipeline


def _github_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def search_windows(max_age_hours: int, now: datetime | None = None) -> list[str]:
    """Фильтры created: для этого запуска: свежее окно всегда, плюс один старый срез по очереди.

    Поиск отдаёт не больше SEARCH_MAX_PAGES * 100 самых новых результатов, а за месяц их
    сотни на запрос. Поэтому месяц режем на срезы по BACKFILL_SLICE_HOURS и в каждый запуск
    дочитываем один из них: за несколько запусков проходится всё окно.
    """
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    fresh = min(FRESH_WINDOW_HOURS, max_age_hours)
    windows = [f"created:>={_github_time(now - timedelta(hours=fresh))}"]
    backfill_hours = max_age_hours - fresh
    if backfill_hours <= 0:
        return windows
    slices = -(-backfill_hours // BACKFILL_SLICE_HOURS)
    # Номер среза зависит только от времени: состояние хранить не нужно, а cron
    # каждые 15 минут перебирает срезы по кругу.
    index = int(now.timestamp() // CRON_PERIOD_SECONDS) % slices
    end = now - timedelta(hours=fresh + index * BACKFILL_SLICE_HOURS)
    start = max(end - timedelta(hours=BACKFILL_SLICE_HOURS), now - timedelta(hours=max_age_hours))
    windows.append(f"created:{_github_time(start)}..{_github_time(end)}")
    return windows


def build_queries(cfg: Config, now: datetime | None = None) -> list[str]:
    return [
        f"is:issue is:open no:assignee {window} {term}"
        for window in search_windows(cfg.max_age_hours, now)
        for term in SEARCH_TERMS
    ]


def collect_candidates(gh: GitHubClient, cfg: Config, seen: SeenStore) -> tuple[dict[str, Candidate], int, int]:
    """Возвращает (кандидаты, число запросов, число упавших запросов)."""
    candidates: dict[str, Candidate] = {}
    queries = build_queries(cfg)
    failed = 0
    for done, query in enumerate(queries):
        try:
            items = gh.search_issues(query)
        except RateLimitError as exc:
            skipped = len(queries) - done
            log.warning("%s — пропускаю оставшиеся %d запросов до следующего запуска", exc, skipped)
            failed += skipped
            break
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.error("Поиск %r упал: %s", query, exc)
            failed += 1
            continue

        new = 0
        for item in items:
            if "pull_request" in item or item.get("assignee") or item.get("assignees"):
                continue
            key = item.get("node_id")
            if not key or key in seen or key in candidates:
                continue
            try:
                candidates[key] = Candidate.from_search_item(item)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Не удалось разобрать issue %s: %s", item.get("html_url"), exc)
                continue
            new += 1
        log.info("Запрос %r: найдено %d, новых %d", query, len(items), new)
    return candidates, len(queries), failed


def filter_candidates(gh: GitHubClient, cfg: Config, seen: SeenStore,
                      candidates: dict[str, Candidate]) -> list[Candidate]:
    # 1. Дешёвые проверки текста — до каких-либо запросов к API.
    clean: list[Candidate] = []
    for cand in candidates.values():
        reason = content_spam_reason(cand)
        if reason:
            log.info("СПАМ %s — %s", cand.url, reason)
            seen.add(cand.key, "spam", cand.url)
        else:
            clean.append(cand)
    if not clean:
        return []

    # 2. Звёзды/форки одним GraphQL-запросом на пачку репозиториев.
    # Такие отказы не запоминаем: репозиторий может набрать звёзды позже.
    repos = gh.fetch_repos(sorted({cand.repo for cand in clean}))
    passed: list[Candidate] = []
    for cand in clean:
        cand.repo_info = repos.get(cand.repo)
        reason = repo_spam_reason(cand, cand.repo_info, cfg)
        if reason:
            log.info("ПРОПУСК %s — %s", cand.url, reason)
        else:
            passed.append(cand)
    if not passed:
        return []

    # 3. Комментарии: конкуренция и сумма из комментариев ботов (Algora/Opire).
    comments = gh.fetch_comments(passed)
    result: list[Candidate] = []
    for cand in passed:
        thread = comments.get(cand.key)
        if thread:
            cand.comments_count = thread["total"]
            competitors = {
                login or f"anon-{idx}"
                for idx, (login, is_bot, body) in enumerate(thread["comments"])
                if not is_bot and login != cand.author and COMPETITION_RE.search(body)
            }
            cand.competitors = len(competitors)
            cand.bot_comments = [body for _, is_bot, body in thread["comments"] if is_bot]

        cand.reward = extract_reward(" ".join(cand.labels), cand.title, strip_code(cand.body),
                                     *(strip_code(body) for body in cand.bot_comments))
        # Не запоминаем: сумму или бота Algora могут добавить позже.
        if cand.reward is None:
            log.info("ПРОПУСК %s — сумма награды не найдена", cand.url)
            continue
        # "$1"/"$5" в тексте — это цены и примеры, а не награда.
        if cand.reward < cfg.min_reward:
            log.info("ПРОПУСК %s — награда %s меньше $%d", cand.url, format_reward(cand.reward), cfg.min_reward)
            continue
        if not has_bounty_signal(cand):
            log.info("ПРОПУСК %s — сумма есть, но нет признаков баунти", cand.url)
            continue

        if cand.competitors > cfg.competition_threshold and cfg.skip_high_competition:
            log.info("ПРОПУСК %s — высокая конкуренция (%d)", cand.url, cand.competitors)
            seen.add(cand.key, "competition", cand.url)
            continue
        result.append(cand)
    return result


def cap_per_repo(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """Самые свежие `limit` задач с каждого репозитория — один репо не должен заваливать чат."""
    kept: list[Candidate] = []
    per_repo: dict[str, int] = {}
    for cand in sorted(candidates, key=lambda c: c.created_at, reverse=True):
        repo = cand.repo.lower()
        if per_repo.get(repo, 0) < limit:
            per_repo[repo] = per_repo.get(repo, 0) + 1
            kept.append(cand)
        else:
            log.info("ПРОПУСК %s — уже %d задач с %s за этот запуск", cand.url, limit, cand.repo)
    return kept


def notify(cfg: Config, seen: SeenStore, candidates: list[Candidate]) -> int:
    candidates = cap_per_repo(candidates, MAX_PER_REPO_PER_RUN)
    candidates.sort(key=lambda c: c.created_at)
    if len(candidates) > cfg.max_messages_per_run:
        log.info("К отправке %d задач, лимит за запуск %d — остальные уйдут в следующий раз",
                 len(candidates), cfg.max_messages_per_run)
        candidates = candidates[-cfg.max_messages_per_run :]

    telegram = None if cfg.dry_run else TelegramClient(cfg.tg_bot_token, cfg.tg_chat_id)
    sent = 0
    for cand in candidates:
        text = format_message(cand, cfg)
        if telegram is None:
            log.info("DRY_RUN, сообщение не отправлено:\n%s", text)
            continue
        if telegram.send(text, card_keyboard(cand)):
            seen.add(cand.key, "sent", cand.url)
            sent += 1
            log.info("Отправлено: %s", cand.url)
    return sent


def run(cfg: Config) -> int:
    seen = SeenStore(cfg.state_file)
    gh = GitHubClient(cfg.github_token)
    exit_code = 0
    try:
        candidates, total_queries, failed = collect_candidates(gh, cfg, seen)
        log.info("Новых кандидатов: %d", len(candidates))
        if failed == total_queries:
            log.error("Все поисковые запросы завершились ошибкой")
            exit_code = 2
        if candidates:
            try:
                ready = filter_candidates(gh, cfg, seen, candidates)
            except (RateLimitError, requests.RequestException, RuntimeError, ValueError) as exc:
                # Кандидаты не помечены как отправленные, поэтому в следующий запуск найдутся снова.
                log.warning("Фильтрация прервана (%s) — доделаем в следующий запуск", exc)
                ready = []
            log.info("Прошли фильтры: %d", len(ready))
            sent = notify(cfg, seen, ready)
            log.info("Отправлено в Telegram: %d", sent)
    finally:
        if cfg.dry_run:
            log.info("DRY_RUN: файл состояния не изменяется")
        else:
            seen.prune(cfg.state_ttl_days)
            seen.save()
    return exit_code


def _handle_sigterm(signum: int, _frame: Any) -> None:
    # Превращаем SIGTERM (отмена/таймаут джобы) в SystemExit, чтобы finally сохранил состояние.
    raise SystemExit(128 + signum)


def main() -> int:
    # Локально на Windows консоль бывает cp1252 — эмодзи в логах иначе роняют вывод.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=getattr(logging, _env_str("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 1
    log.info("Старт: языки=%s, окно=%d ч, звёзд>=%d, форков>=%d, dry_run=%s",
             ",".join(cfg.languages), cfg.max_age_hours, cfg.min_stars, cfg.min_forks, cfg.dry_run)
    try:
        return run(cfg)
    except Exception:
        log.exception("Необработанная ошибка")
        return 1


if __name__ == "__main__":
    sys.exit(main())
