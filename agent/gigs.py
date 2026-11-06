"""Заказы вне GitHub: Superteam Earn и Freelancer.com.

Модуль общий для сканера (main.py ищет заказы и шлёт карточки) и агента
(по кнопке «✍️ отклик» берёт полное описание и пишет черновик отклика).
Код по таким заказам бот не пишет и откликается не сам: черновик копирует человек.

Оба API открыты без ключа (проверено 2026-11-06):
- Superteam: GET earn.superteam.fun/api/listings?context=all&category=…&status=open,
  описание — GET /api/listings/details/<slug>;
- Freelancer: GET www.freelancer.com/api/projects/0.1/projects/active/?jobs[]=…,
  лимит без ключа 200 запросов в минуту и 1000 в час (заголовок ratelimit-limit).
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

TIMEOUT = 20
HEADERS = {"Accept": "application/json", "User-Agent": "bounty-hunter-bot"}

SUPERTEAM_API = "https://earn.superteam.fun/api"
SUPERTEAM_WEB = "https://superteam.fun/earn/listing"
# Допустимые значения category, их перечисляет ошибка валидации API.
SUPERTEAM_CATEGORIES = ("All", "Content", "Design", "Development", "Other")
# Стейблкоины считаем 1:1 к доллару; у остальных токенов цену не знаем.
STABLE_TOKENS = frozenset({"USDC", "USDG", "USDT", "PYUSD", "USD"})

FREELANCER_API = "https://www.freelancer.com/api/projects/0.1"
FREELANCER_WEB = "https://www.freelancer.com/projects"
# id категорий Freelancer (GET /api/projects/0.1/jobs/?job_names[]=…).
FREELANCER_CODE_JOBS = (320, 13, 1265, 248, 1002, 31, 3278)  # C++, Python, Rust, Go, Docker, Linux, Audio Programming
FREELANCER_MUSIC_JOBS = (2254, 693, 3561, 2173, 1984, 3304)  # Music Production, Audio Production, Mixing & Mastering,
                                                               # Audio Mastering, Mixing Engineering, Game Music
FREELANCER_DEFAULT_JOBS = FREELANCER_CODE_JOBS + FREELANCER_MUSIC_JOBS
FREELANCER_PAGE = 100

# Типичные мошеннические заказы, которые попадались в выдаче.
SCAM_RE = re.compile(r"flash\s*usdt|untraceable|fake\s+(?:review|id)|seed\s+phrase|"
                     r"(?:bypass|crack)\s+(?:kyc|license|captcha)", re.IGNORECASE)

PLATFORM_NAMES = {"st": "Superteam Earn", "fl": "Freelancer"}
MAX_DESCRIPTION = 6000


class GigError(Exception):
    pass


@dataclass(frozen=True)
class Gig:
    prefix: str  # "st" | "fl"
    id: str
    title: str
    url: str
    reward: str  # как показать в карточке
    reward_usd: float | None  # для фильтра; None — цена токена неизвестна
    competitors: int  # отправленных работ / ставок
    created_at: datetime | None
    deadline: datetime | None
    skills: tuple[str, ...] = ()
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.prefix}:{self.id}"

    @property
    def platform(self) -> str:
        return PLATFORM_NAMES[self.prefix]


def is_gig_key(key: str) -> bool:
    prefix, sep, rest = key.partition(":")
    return bool(sep and rest) and prefix in PLATFORM_NAMES


def _get(session: requests.Session, url: str, params: Any = None) -> Any:
    try:
        resp = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise GigError(f"{url}: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise GigError(f"{url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GigError(f"{url}: ответ не JSON") from exc


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def html_to_text(value: str) -> str:
    text = re.sub(r"<(?:br|/p|/li|/h\d)[^>]*>", "\n", value or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _money(amount: float) -> str:
    return f"{amount:,.0f}" if amount >= 100 or amount == int(amount) else f"{amount:,.2f}"


# ---------------------------------------------------------------- Superteam


def parse_superteam(payload: Any, now: datetime) -> list[Gig]:
    gigs: list[Gig] = []
    for item in payload if isinstance(payload, list) else []:
        try:
            deadline = _iso(item.get("deadline"))
            if (item.get("status") != "OPEN" or item.get("isWinnersAnnounced")
                    or (deadline is not None and deadline <= now)):
                continue
            token = str(item.get("token") or "")
            amount = item.get("rewardAmount") or item.get("maxRewardAsk") or 0
            amount = float(amount)
            if item.get("compensationType") == "range" and item.get("minRewardAsk"):
                reward = f"{_money(float(item['minRewardAsk']))}–{_money(amount)} {token}"
            elif amount:
                reward = f"{_money(amount)} {token}"
            else:
                reward = "по договорённости"
            reward_usd = amount if token.upper() in STABLE_TOKENS else None
            counts = item.get("_count") or {}
            access = item.get("agentAccess") or ""
            sponsor = (item.get("sponsor") or {}).get("name") or ""
            note = " · ".join(filter(None, [
                {"bounty": "конкурс", "project": "проект", "hackathon": "хакатон"}.get(item.get("type"), ""),
                sponsor,
                "🤖 можно агенту" if access in ("AGENT_ALLOWED", "AGENT_ONLY") else "",
            ]))
            gigs.append(Gig(
                prefix="st", id=str(item["id"]), title=str(item.get("title") or "").strip(),
                url=f"{SUPERTEAM_WEB}/{item['slug']}", reward=reward, reward_usd=reward_usd,
                competitors=int(counts.get("Submission") or 0), created_at=None, deadline=deadline,
                note=note,
            ))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return gigs


def fetch_superteam(session: requests.Session, category: str = "All",
                    now: datetime | None = None) -> list[Gig]:
    payload = _get(session, f"{SUPERTEAM_API}/listings",
                   {"context": "all", "category": category, "status": "open"})
    return parse_superteam(payload, now or datetime.now(timezone.utc))


def superteam_detail(session: requests.Session, listing_id: str) -> dict[str, Any]:
    # Описание отдаётся только по slug, а в callback помещается лишь id: ищем slug в ленте.
    slug = ""
    for status in ("open", "all"):
        feed = _get(session, f"{SUPERTEAM_API}/listings",
                    {"context": "all", "category": "All", "status": status})
        slug = next((x.get("slug") for x in feed if isinstance(x, dict) and x.get("id") == listing_id), "")
        if slug:
            break
    if not slug:
        raise GigError("заказ не найден, возможно его сняли")
    data = _get(session, f"{SUPERTEAM_API}/listings/details/{slug}")
    skills = [s.get("skills", "") for s in data.get("skills") or [] if isinstance(s, dict)]
    questions = [str(q.get("question") or "") for q in data.get("eligibility") or [] if isinstance(q, dict)]
    return {
        "platform": PLATFORM_NAMES["st"],
        "title": str(data.get("title") or ""),
        "url": f"{SUPERTEAM_WEB}/{slug}",
        "budget": f"{data.get('rewardAmount') or ''} {data.get('token') or ''}".strip(),
        "skills": [s for s in skills if s],
        "description": html_to_text(str(data.get("description") or ""))[:MAX_DESCRIPTION],
        "requirements": html_to_text(str(data.get("requirements") or ""))[:1500],
        "questions": [q for q in questions if q],
        "agent_access": str(data.get("agentAccess") or ""),
    }


# --------------------------------------------------------------- Freelancer


def _freelancer_budget(project: dict[str, Any]) -> tuple[str, float | None]:
    budget = project.get("budget") or {}
    currency = project.get("currency") or {}
    code = str(currency.get("code") or "")
    rate = float(currency.get("exchange_rate") or 0)
    low, high = budget.get("minimum"), budget.get("maximum")
    hourly = "/ч" if project.get("type") == "hourly" else ""
    parts = [_money(float(v)) for v in (low, high) if v]
    text = ("–".join(parts) if parts else "?") + f" {code}{hourly}"
    top = float(high or low or 0)
    usd = top * rate if rate else None
    if usd is not None and code != "USD":
        text += f" (≈${_money(usd)}{hourly})"
    return text, usd


def parse_freelancer(payload: Any, jobs: tuple[int, ...] = ()) -> list[Gig]:
    projects = ((payload or {}).get("result") or {}).get("projects") if isinstance(payload, dict) else None
    gigs: list[Gig] = []
    for p in projects or []:
        try:
            if p.get("nonpublic") or p.get("deleted") or p.get("status") != "active":
                continue
            # API отдаёт и заказы из соседних категорий (хореография по фильтру «музыка»):
            # оставляем только те, где есть одна из выбранных категорий.
            if jobs and not {j.get("id") for j in p.get("jobs") or [] if isinstance(j, dict)} & set(jobs):
                continue
            reward, usd = _freelancer_budget(p)
            skills = tuple(j.get("name", "") for j in p.get("jobs") or [] if isinstance(j, dict))
            notes = []
            if p.get("is_seller_kyc_required"):
                notes.append("нужна верификация личности")
            if p.get("type") == "hourly":
                notes.append("почасовая")
            gigs.append(Gig(
                prefix="fl", id=str(p["id"]), title=str(p.get("title") or "").strip(),
                url=f"{FREELANCER_WEB}/{p['seo_url']}", reward=reward, reward_usd=usd,
                competitors=int((p.get("bid_stats") or {}).get("bid_count") or 0),
                created_at=datetime.fromtimestamp(int(p["time_submitted"]), tz=timezone.utc),
                deadline=None, skills=tuple(s for s in skills if s), note=" · ".join(notes),
            ))
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    return gigs


def fetch_freelancer(session: requests.Session, jobs: tuple[int, ...] = FREELANCER_DEFAULT_JOBS) -> list[Gig]:
    params: list[tuple[str, Any]] = [("jobs[]", j) for j in jobs]
    params += [("limit", FREELANCER_PAGE), ("sort_field", "time_updated"),
               ("job_details", "true"), ("compact", "true")]
    return parse_freelancer(_get(session, f"{FREELANCER_API}/projects/active/", params), jobs)


def freelancer_detail(session: requests.Session, project_id: str) -> dict[str, Any]:
    if not project_id.isdigit():
        raise GigError("неверный id заказа")
    data = _get(session, f"{FREELANCER_API}/projects/{project_id}/",
                {"full_description": "true", "job_details": "true"})
    project = (data or {}).get("result") or {}
    if not project or project.get("status") != "active":
        raise GigError("заказ закрыт или удалён")
    budget, _ = _freelancer_budget(project)
    return {
        "platform": PLATFORM_NAMES["fl"],
        "title": str(project.get("title") or ""),
        "url": f"{FREELANCER_WEB}/{project.get('seo_url')}",
        "budget": budget,
        "skills": [j.get("name", "") for j in project.get("jobs") or [] if isinstance(j, dict)],
        "description": str(project.get("description") or project.get("preview_description") or "")[:MAX_DESCRIPTION],
        "requirements": "",
        "questions": [],
        "bids": int((project.get("bid_stats") or {}).get("bid_count") or 0),
    }


def fetch_detail(key: str, session: requests.Session | None = None) -> dict[str, Any]:
    """Полное описание заказа по ключу карточки ("st:<uuid>" или "fl:<id>")."""
    prefix, _, gig_id = key.partition(":")
    session = session or requests.Session()
    if prefix == "st":
        return superteam_detail(session, gig_id)
    if prefix == "fl":
        return freelancer_detail(session, gig_id)
    raise GigError(f"неизвестная площадка в ключе {key!r}")


# ------------------------------------------------------------------ filters


@dataclass(frozen=True)
class GigFilter:
    min_usd: float = 30
    max_competitors: int = 30
    max_age_hours: int = 24

    def reason(self, gig: Gig, now: datetime) -> str | None:
        """Почему заказ не подходит, или None."""
        if SCAM_RE.search(gig.title):
            return "похоже на мошенничество"
        if gig.reward_usd is not None and gig.reward_usd < self.min_usd:
            return f"бюджет меньше ${self.min_usd:g}"
        # На Superteam конкурсы с призами для нескольких мест: число работ показываем, но не режем.
        if gig.prefix == "fl" and gig.competitors > self.max_competitors:
            return f"уже {gig.competitors} откликов"
        if gig.created_at is not None and (now - gig.created_at).total_seconds() > self.max_age_hours * 3600:
            return f"старше {self.max_age_hours} ч"
        return None


def humanize_left(deadline: datetime, now: datetime) -> str:
    hours = max(0, int((deadline - now).total_seconds() // 3600))
    return f"{hours // 24} д {hours % 24} ч" if hours >= 24 else f"{hours} ч"


def format_card(gig: Gig, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    esc = html.escape
    title = gig.title if len(gig.title) <= 200 else gig.title[:197] + "…"
    icon = "🎵" if any("music" in s.lower() or "audio" in s.lower() or "mix" in s.lower()
                      for s in gig.skills) else "💼"
    lines = [
        f'{icon} <b><a href="{esc(gig.url)}">{esc(title)}</a></b>',
        f"🏷 {esc(gig.platform)}" + (f" · {esc(gig.note)}" if gig.note else ""),
        f"💰 {esc(gig.reward)}",
        f"👥 {'работ отправлено' if gig.prefix == 'st' else 'ставок'}: {gig.competitors}",
    ]
    if gig.skills:
        lines.append(f"🧩 {esc(', '.join(gig.skills[:5]))}")
    if gig.deadline is not None:
        lines.append(f"⏳ до дедлайна {humanize_left(gig.deadline, now)}")
    if gig.created_at is not None:
        minutes = int((now - gig.created_at).total_seconds() // 60)
        lines.append(f"🕒 опубликован {minutes // 60} ч {minutes % 60} мин назад" if minutes >= 60
                     else f"🕒 опубликован {max(minutes, 0)} мин назад")
    return "\n".join(lines)


def card_keyboard(gig: Gig) -> dict[str, Any] | None:
    write, skip = f"gig:{gig.key}", f"skip:{gig.key}"
    if len(write.encode()) > 64:  # лимит callback_data в Telegram
        return None
    return {"inline_keyboard": [[
        {"text": "✍️ отклик", "callback_data": write},
        {"text": "пропустить", "callback_data": skip},
    ]]}


def collect(session: requests.Session, sources: tuple[str, ...], flt: GigFilter,
            superteam_category: str = "All", freelancer_jobs: tuple[int, ...] = FREELANCER_DEFAULT_JOBS,
            now: datetime | None = None, log: Any = None) -> list[Gig]:
    """Заказы со всех включённых площадок, прошедшие фильтр. Сбой одной площадки не мешает другой."""
    now = now or datetime.now(timezone.utc)
    found: list[Gig] = []
    for source in sources:
        started = time.monotonic()
        try:
            if source == "superteam":
                gigs = fetch_superteam(session, superteam_category, now)
            elif source == "freelancer":
                gigs = fetch_freelancer(session, freelancer_jobs)
            else:
                continue
        except GigError as exc:
            if log:
                log.warning("Заказы %s не загружены: %s", source, exc)
            continue
        kept = 0
        for gig in gigs:
            why = flt.reason(gig, now)
            if why:
                if log:
                    log.debug("ПРОПУСК %s — %s", gig.url, why)
                continue
            found.append(gig)
            kept += 1
        if log:
            log.info("%s: заказов %d, подходят %d (%.1f с)", source, len(gigs), kept, time.monotonic() - started)
    return found
