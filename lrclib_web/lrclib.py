"""LRCLIB HTTP client with pacing, Retry-After handling and a circuit breaker (ARCHITECTURE.md §6)."""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import __version__

MAX_GAP_S = 5.0  # cap for the adaptive request gap
OK_STREAK_TO_RELAX = 20

USER_AGENT = f"lrclib-web/{__version__} (+https://github.com/vantuzz/lrclib-web)"


class Stopped(Exception):
    """Raised when the stop event interrupts a wait."""


class LrclibUnavailable(Exception):
    """Breaker opened three times in one run."""


@dataclass
class Response:
    status: int  # HTTP status, or 0 for network error
    data: object = None  # parsed JSON or None
    error: str | None = None
    transient: bool = False


class Transport:
    """Thin wrapper around urllib so tests can replace it."""

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:  # noqa: BLE001
                pass
            return e.code, dict(e.headers.items()) if e.headers else {}, body


def _retry_after(headers: dict[str, str]) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            v = v.strip()
            try:
                return max(0.0, float(v))
            except ValueError:
                pass
            try:  # HTTP-date form
                dt = email.utils.parsedate_to_datetime(v)
            except (TypeError, ValueError):
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0.0, (dt - datetime.now(UTC)).total_seconds())
    return None


class LrclibClient:
    def __init__(
        self,
        base_url: str = "https://lrclib.net",
        app_id: str = "",
        gap_ms: int = 500,
        jitter_ms: int = 250,
        stop: threading.Event | None = None,
        log: Callable[[str, str], None] = lambda level, msg: None,
        transport: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        breaker_pause_s: float = 15 * 60,
        on_cooldown: Callable[[float | None], None] = lambda until: None,
    ):
        self.base = base_url.rstrip("/")
        host = urllib.parse.urlsplit(self.base).hostname or ""
        self.app_id = app_id if (app_id and host.endswith("lrclib.net")) else ""
        self.gap = max(0.2, gap_ms / 1000.0)  # configured gap
        self.cur_gap = self.gap  # adaptive: doubled on 429, relaxed after a streak of successes
        self.ok_streak = 0
        self.jitter = max(0.0, jitter_ms / 1000.0)
        self.stop = stop or threading.Event()
        self.log = log
        self.transport = transport or Transport()
        self.clock = clock
        self.rand = rand
        self.breaker_pause_s = breaker_pause_s
        self.on_cooldown = on_cooldown
        self._last_request: float | None = None
        self.consecutive_failures = 0
        self.wait_429_total = 0.0
        self.breaker_opens = 0
        self.requests = 0

    # -- waiting ---------------------------------------------------------
    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.stop.wait(seconds):
            raise Stopped()

    def _pace(self) -> None:
        if self._last_request is None:
            return
        target = self.cur_gap + self.rand() * self.jitter
        elapsed = self.clock() - self._last_request
        self._sleep(target - elapsed)

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.app_id:
            h["Lrclib-App-Id"] = self.app_id
        return h

    def _slow_down(self) -> None:
        self.ok_streak = 0
        self.cur_gap = min(max(MAX_GAP_S, self.gap), self.cur_gap * 2)

    def _ok(self) -> None:
        self.ok_streak += 1
        if self.ok_streak >= OK_STREAK_TO_RELAX:
            self.ok_streak = 0
            self.cur_gap = max(self.gap, self.cur_gap / 2)

    # -- breaker ---------------------------------------------------------
    def _failure(self) -> None:
        self.ok_streak = 0
        self.consecutive_failures += 1
        if self.consecutive_failures >= 5 or self.wait_429_total > 600:
            self._open_breaker()

    def _open_breaker(self) -> None:
        self.breaker_opens += 1
        self.consecutive_failures = 0
        self.wait_429_total = 0.0
        if self.breaker_opens >= 3:
            raise LrclibUnavailable("LRCLIB unavailable")
        self.log(
            "warn", f"LRCLIB failing, pausing {int(self.breaker_pause_s // 60)} min (breaker {self.breaker_opens}/3)"
        )
        self.on_cooldown(time.time() + self.breaker_pause_s)
        try:
            self._sleep(self.breaker_pause_s)
        finally:
            self.on_cooldown(None)

    # -- request ---------------------------------------------------------
    def request(self, path: str, params: dict[str, object]) -> Response:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base}{path}?{query}" if query else f"{self.base}{path}"
        attempts_429 = 0
        attempts_503 = 0
        attempts_5xx = 0
        wait_429 = 10.0
        while True:
            if self.stop.is_set():
                raise Stopped()
            self._pace()
            self._last_request = self.clock()
            self.requests += 1
            try:
                status, headers, body = self.transport(url, self._headers(), 30.0)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                status, headers, body = 0, {}, b""
                net_err = f"network: {getattr(e, 'reason', e)}"
            else:
                net_err = None

            if status in (429, 503):
                attempts_429 += 1
                if status == 429:
                    self._slow_down()
                else:
                    self.ok_streak = 0
                ra = _retry_after(headers)
                if ra is not None:
                    base = max(ra, 1.0)
                elif status == 429:
                    base = wait_429
                    wait_429 = min(wait_429 * 2, 120.0)
                else:
                    attempts_503 += 1
                    base = float(2 ** min(attempts_503 - 1, 4))  # 1, 2, 4, 8, 16 s
                if attempts_429 >= 6:
                    self._failure()
                    return Response(status, error=f"HTTP {status} after retries", transient=True)
                delay = base * (1 + self.rand() * 0.3)
                if status == 429:
                    self.wait_429_total += delay
                self.log("warn", f"LRCLIB {status}, retry in {delay:.1f} s")
                self.on_cooldown(time.time() + delay)
                try:
                    self._sleep(delay)
                finally:
                    self.on_cooldown(None)
                if self.wait_429_total > 600:
                    self._open_breaker()
                continue

            if status == 0 or status in (500, 502, 504):
                self.ok_streak = 0
                attempts_5xx += 1
                if attempts_5xx >= 4:
                    self._failure()
                    return Response(status, error=net_err or f"HTTP {status}", transient=True)
                delay = 0.5 * (2 ** (attempts_5xx - 1)) * (1 + self.rand() * 0.3)
                self._sleep(delay)
                continue

            if status == 401 and self.app_id and b"InvalidAppId" in body:
                self.log("error", "LRCLIB rejected LRCLIB_APP_ID; continuing without it")
                self.app_id = ""
                return Response(status, error="invalid app id", transient=True)

            if status == 404:
                self.consecutive_failures = 0
                self._ok()
                return Response(404)

            parsed = None
            try:
                parsed = json.loads(body.decode("utf-8")) if body else None
            except (ValueError, UnicodeDecodeError):
                parsed = None

            if status == 200:
                if parsed is None:
                    self._failure()
                    return Response(200, error="non-JSON 200 response", transient=True)
                self.consecutive_failures = 0
                self._ok()
                return Response(200, data=parsed)

            if status == 400:
                msg = parsed.get("message") if isinstance(parsed, dict) else None
                self.consecutive_failures = 0
                return Response(400, error=f"bad request: {msg or 'HTTP 400'}", transient=False)

            self._failure()
            return Response(status, error=f"HTTP {status}", transient=True)

    def get(self, title: str, artist: str, album: str | None, duration_s: int | None) -> Response:
        params: dict[str, object] = {"track_name": title, "artist_name": artist, "album_name": album or ""}
        if duration_s and 1 <= duration_s <= 3600:
            params["duration"] = duration_s
        return self.request("/api/get", params)

    def search(self, title: str, artist: str) -> Response:
        return self.request("/api/search", {"track_name": title, "artist_name": artist})
