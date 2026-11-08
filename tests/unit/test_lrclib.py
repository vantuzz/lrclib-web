import email.utils
import json
import threading
import time

import pytest

from lrclib_web.lrclib import USER_AGENT, LrclibClient, LrclibUnavailable, Stopped, _retry_after


def ok(obj):
    return 200, {}, json.dumps(obj).encode()


class Harness:
    def __init__(self, responses, **kw):
        self.responses = list(responses)
        self.calls = []  # (url, headers)
        self.delays = []
        self._t = 0.0
        kw.setdefault("rand", lambda: 0.0)
        self.client = LrclibClient(transport=self._transport, clock=self._clock, **kw)
        self.client._sleep = lambda s: self.delays.append(s) if s > 0 else None

    def _clock(self):
        self._t += 1000.0  # always far past the pacing gap
        return self._t

    def _transport(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def test_ok_and_user_agent():
    h = Harness([ok({"id": 1})])
    r = h.client.get("T", "A", "Al", 200)
    assert r.status == 200 and r.data == {"id": 1}
    url, headers = h.calls[0]
    assert headers["User-Agent"] == USER_AGENT and "lrclib-web/" in USER_AGENT
    assert "track_name=T" in url and "duration=200" in url


def test_get_omits_bad_duration():
    h = Harness([ok({})])
    h.client.get("T", "A", None, 0)
    assert "duration" not in h.calls[0][0]


def test_429_retry_after():
    h = Harness([(429, {"Retry-After": "7"}, b""), ok({"id": 1})])
    r = h.client.request("/api/get", {})
    assert r.status == 200 and h.delays == [7.0]


def test_429_without_header_doubles():
    h = Harness([(429, {}, b"")] * 3 + [ok({"id": 1})])
    r = h.client.request("/api/get", {})
    assert r.status == 200 and h.delays == [10.0, 20.0, 40.0]


def test_429_gives_up_after_six():
    h = Harness([(429, {}, b"")])
    r = h.client.request("/api/get", {})
    assert r.status == 429 and r.transient and len(h.calls) == 6


def test_503_default_one_second():
    h = Harness([(503, {}, b""), ok({})])
    assert h.client.request("/x", {}).status == 200
    assert h.delays == [1.0]


def test_503_exponential_without_retry_after():
    h = Harness([(503, {}, b"")])
    r = h.client.request("/x", {})
    assert r.status == 503 and r.transient and len(h.calls) == 6
    assert h.delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_503_retry_after_header_wins():
    h = Harness([(503, {"Retry-After": "3"}, b""), ok({})])
    h.client.request("/x", {})
    assert h.delays == [3.0]


def test_retry_after_http_date():
    soon = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 27 <= _retry_after({"retry-after": soon}) <= 30
    past = email.utils.formatdate(time.time() - 3600, usegmt=True)
    assert _retry_after({"Retry-After": past}) == 0.0
    assert _retry_after({"Retry-After": "garbage"}) is None
    assert _retry_after({}) is None


def test_429_http_date_used_for_delay():
    soon = email.utils.formatdate(time.time() + 30, usegmt=True)
    h = Harness([(429, {"Retry-After": soon}, b""), ok({})])
    h.client.request("/x", {})
    assert 27 <= h.delays[0] <= 30


def test_adaptive_gap_doubles_on_429_caps_and_relaxes():
    h = Harness([(429, {"Retry-After": "0"}, b""), ok({})], gap_ms=500)
    c = h.client
    assert c.cur_gap == 0.5
    c.request("/x", {})
    assert c.cur_gap == 1.0
    for _ in range(5):
        c._slow_down()
    assert c.cur_gap == 5.0  # capped
    h.responses[:] = [ok({})]
    for _ in range(19):
        c.request("/x", {})
    assert c.cur_gap == 5.0
    c.request("/x", {})
    assert c.cur_gap == 2.5  # halved after 20 successes
    for _ in range(100):
        c.request("/x", {})
    assert c.cur_gap == 0.5  # never below configured gap


def test_404_counts_as_success_and_failure_resets_streak():
    h = Harness([(404, {}, b"")], gap_ms=500)
    c = h.client
    c.cur_gap = 2.0
    for _ in range(19):
        c.request("/x", {})
    h.responses[:] = [(500, {}, b"")]
    c.request("/x", {})
    h.responses[:] = [(404, {}, b"")]
    c.request("/x", {})
    assert c.cur_gap == 2.0 and c.ok_streak == 1


def test_pacing_uses_adaptive_gap():
    delays = []
    t = [0.0]
    c = LrclibClient(gap_ms=500, jitter_ms=0, transport=lambda u, h, to: ok({}), clock=lambda: t[0])
    c._sleep = delays.append
    c.cur_gap = 2.0
    c.request("/x", {})
    t[0] = 0.5
    c.request("/x", {})
    assert delays == [pytest.approx(1.5)]


def test_5xx_backoff_max_four_attempts():
    h = Harness([(500, {}, b"")])
    r = h.client.request("/x", {})
    assert r.transient and r.status == 500
    assert len(h.calls) == 4
    assert h.delays == [0.5, 1.0, 2.0]


def test_5xx_recovers():
    h = Harness([(502, {}, b""), ok({"a": 1})])
    assert h.client.request("/x", {}).data == {"a": 1}


def test_network_error_transient():
    h = Harness([OSError("boom")])
    r = h.client.request("/x", {})
    assert r.status == 0 and r.transient and len(h.calls) == 4


def test_non_json_200_is_transient_error():
    h = Harness([(200, {}, b"<html>")])
    r = h.client.request("/x", {})
    assert r.status == 200 and r.transient and r.error and r.data is None


def test_400_permanent():
    h = Harness([(400, {}, json.dumps({"message": "bad"}).encode())])
    r = h.client.request("/x", {})
    assert r.status == 400 and not r.transient and "bad" in r.error
    assert len(h.calls) == 1


def test_404():
    h = Harness([(404, {}, b"{}")])
    r = h.client.request("/x", {})
    assert r.status == 404 and r.error is None and not r.transient


def test_invalid_app_id_dropped():
    h = Harness([(401, {}, b'{"name":"InvalidAppId"}'), ok({})], base_url="https://lrclib.net", app_id="abc")
    assert h.client._headers().get("Lrclib-App-Id") == "abc"
    r = h.client.request("/x", {})
    assert r.status == 401 and r.transient
    h.client.request("/x", {})
    assert h.calls[0][1].get("Lrclib-App-Id") == "abc"
    assert "Lrclib-App-Id" not in h.calls[1][1]


def test_app_id_only_for_lrclib_host():
    c = LrclibClient(base_url="http://127.0.0.1:1", app_id="abc")
    assert "Lrclib-App-Id" not in c._headers()


def test_breaker_raises_on_third_open():
    pauses = []
    h = Harness([(500, {}, b"")], on_cooldown=lambda u: pauses.append(u))
    # each failed request counts one failure; 5 failures open the breaker
    for _ in range(14):
        assert h.client.request("/x", {}).transient
    assert h.client.breaker_opens == 2
    with pytest.raises(LrclibUnavailable):
        h.client.request("/x", {})
    assert h.client.breaker_opens == 3
    assert h.client.breaker_pause_s in h.delays


def test_success_resets_failures():
    h = Harness([(500, {}, b"")] * 4 + [ok({})])
    h.client.request("/x", {})
    assert h.client.consecutive_failures == 1
    h.client.request("/x", {})
    assert h.client.consecutive_failures == 0


def test_stop_event_raises_stopped():
    stop = threading.Event()
    stop.set()
    h = Harness([ok({})], stop=stop)
    with pytest.raises(Stopped):
        h.client.request("/x", {})
    assert h.calls == []


def test_real_sleep_interrupted_by_stop():
    stop = threading.Event()
    c = LrclibClient(stop=stop)
    stop.set()
    with pytest.raises(Stopped):
        c._sleep(5)


def test_pacing_gap_with_fake_clock():
    delays = []
    t = [0.0]
    c = LrclibClient(gap_ms=500, jitter_ms=0, transport=lambda u, h, to: ok({}), clock=lambda: t[0])
    c._sleep = delays.append
    c.request("/x", {})
    assert delays == []  # first request never waits
    t[0] = 0.1
    c.request("/x", {})
    assert delays == [pytest.approx(0.4)]
