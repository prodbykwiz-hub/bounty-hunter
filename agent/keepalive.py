"""HTTP-эндпоинт и самопинг для бесплатного веб-сервиса Render.

Бесплатный сервис Render засыпает после 15 минут без входящих HTTP-запросов,
а агент сам ходит в Telegram и входящих запросов не получает. Поэтому он
отвечает на /health и раз в несколько минут сам запрашивает свой публичный адрес.
Если сервис всё же уснул, его будит сканер в GitHub Actions (шаг ping agent).
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

log = logging.getLogger("bounty_agent")

PING_INTERVAL = 5 * 60


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args: object) -> None:
        # Пинги каждые 5 минут засоряли бы лог.
        pass


def start_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("HTTP для проверки живости на порту %d", port)
    return server


def _ping_loop(url: str, interval: int) -> None:
    session = requests.Session()
    while True:
        time.sleep(interval)
        try:
            session.get(url, timeout=30)
        except requests.RequestException as exc:
            log.warning("Самопинг %s не прошёл: %s", url, type(exc).__name__)


def start_self_ping(public_url: str, interval: int = PING_INTERVAL) -> None:
    url = public_url.rstrip("/") + "/health"
    threading.Thread(target=_ping_loop, args=(url, interval), name="self-ping", daemon=True).start()
    log.info("Самопинг %s раз в %d с", url, interval)
