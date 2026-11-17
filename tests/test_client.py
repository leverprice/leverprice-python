from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import leverprice
from _support import (
    CHROME,
    GOOGLEBOT,
    GUARDS,
    PIN,
    SECRET,
    Call,
    Clock,
    FakeServer,
    Headers,
    SigningKeys,
    b64url_decode,
    config_payload,
    decode_cookie,
    default_products,
    make_client,
    wait_for,
    wait_idle,
)
from leverprice import (
    MIN_KEYSET_VERSION,
    ROOT_PUBLIC_KEYS,
    SDK_VERSION,
    Leverprice,
    ProductGuard,
    TransportResponse,
    UnknownProductError,
    _client,
)
from leverprice._ed25519 import decode_point

KEYS = SigningKeys()


@pytest.fixture
def server() -> FakeServer:
    return FakeServer(KEYS.envelope(config_payload()))


@pytest.fixture
def errors() -> List[Exception]:
    return []


def messages(errors: List[Exception]) -> List[str]:
    return [str(e) for e in errors]


def new_visitor() -> Dict[str, str]:
    return {"user-agent": CHROME}


def test_public_constants() -> None:
    assert dict(ROOT_PUBLIC_KEYS) == {"r1": "5FmNzXvPMPZEuDnFT_fFd_VVNsUrWwWTKLerMWbSdcg"}
    for key in ROOT_PUBLIC_KEYS.values():
        raw = b64url_decode(key)
        assert len(raw) == 32
        assert decode_point(raw) is not None
    assert MIN_KEYSET_VERSION == 1
    assert SDK_VERSION == leverprice.__version__ == "0.3.0"
    with pytest.raises(TypeError):
        ROOT_PUBLIC_KEYS["r2"] = "x"  # type: ignore[index]


def test_request_headers_and_body(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors, api_url="https://api.example.test///")
    quote = lp.get_price("pro", new_visitor())
    lp.flush()
    assert quote.reason == "experiment"
    get, post = server.config_calls[0], server.event_calls[0]
    assert get.url == "https://api.example.test/v1/config"
    assert post.url == "https://api.example.test/v1/events"
    for call in (get, post):
        assert call.headers["Authorization"] == f"Bearer {SECRET}"
        assert call.headers["User-Agent"] == "leverprice-sdk-python/0.3.0"
        assert not any(k.lower() == "origin" for k in call.headers)
    assert "If-None-Match" not in get.headers
    assert post.headers["Content-Type"] == "application/json"
    assert json.loads(post.body or b"")["sdk"] == {"name": "leverprice-python", "version": "0.3.0"}
    assert errors == []


def test_cold_start_times_out(server: FakeServer, errors: List[Exception]) -> None:
    release = threading.Event()

    def slow(call: Call) -> TransportResponse:
        release.wait(5)
        return TransportResponse(503, {}, b"")

    server.config_handler = slow
    lp = make_client(server, errors=errors, timeout_ms=150)
    ends: List[float] = []
    quotes: List[Any] = []

    def visit(delay: float) -> None:
        time.sleep(delay)
        quotes.append(lp.get_price("pro", new_visitor()))
        ends.append(time.monotonic())

    start = time.monotonic()
    threads = [threading.Thread(target=visit, args=(d,)) for d in (0, 0.04, 0.08)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    release.set()
    assert all(q.reason == "no_config" and q.set_cookie == [PIN] and q.price == 1900 for q in quotes)
    assert 0.12 <= max(ends) - start < 1.0
    assert max(ends) - min(ends) < 0.1
    assert len(server.config_calls) == 1


def test_outage_pins_the_fallback(server: FakeServer, errors: List[Exception]) -> None:
    server.config_handler = lambda call: TransportResponse(503, {}, b'{"error":{"code":"unavailable"}}')
    lp = make_client(server, errors=errors)
    quote = lp.get_price("pro", new_visitor())
    assert (quote.reason, quote.is_fallback, quote.price, quote.set_cookie) == ("no_config", True, 1900, [PIN])
    assert messages(errors) == ["Leverprice config: HTTP 503 (unavailable)"]

    started = time.monotonic()
    again = lp.get_price("pro", new_visitor())
    assert again.reason == "no_config"
    assert time.monotonic() - started < 0.1
    assert len(server.config_calls) == 1

    pinned = lp.get_price("pro", {"user-agent": CHROME, "cookie": "__pp_f=1"})
    assert (pinned.reason, pinned.set_cookie, pinned.price) == ("pinned_fallback", [PIN], 1900)


def test_pin_applies_to_the_rest_of_the_request(server: FakeServer, errors: List[Exception]) -> None:
    server.config_handler = lambda call: TransportResponse(500, {}, b"")
    lp = make_client(server, errors=errors)
    request = Headers(new_visitor())
    assert lp.get_price("pro", request).reason == "no_config"
    server.config_handler = server._serve_config
    lp.refresh()
    assert lp.get_price("team", request).reason == "pinned_fallback"
    assert lp.get_price("team", Headers(new_visitor())).reason == "experiment"


def test_pinned_visitor_keeps_the_config_warm(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    quote = lp.get_price("pro", {"user-agent": CHROME, "cookie": "a=1; __pp_f=1"})
    assert quote.reason == "pinned_fallback"
    wait_idle(lp)
    assert len(server.config_calls) == 1
    assert lp.get_price("pro", new_visitor()).reason == "experiment"


def test_backoff_and_preload_reset(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    server.config_handler = lambda call: TransportResponse(502, {}, b"")
    lp = make_client(server, errors=errors)
    lp._clock = clock
    lp.preload()
    assert len(server.config_calls) == 1
    assert lp._retry_at == 0
    assert lp.get_price("pro", new_visitor()).reason == "no_config"
    assert len(server.config_calls) == 2
    assert lp._retry_at == clock.now + 2000
    lp.get_price("pro", new_visitor())
    assert len(server.config_calls) == 2
    clock.advance(2000)
    lp.get_price("pro", new_visitor())
    assert len(server.config_calls) == 3
    assert lp._retry_at == clock.now + 4000
    lp.refresh()
    assert len(server.config_calls) == 4


def test_304_revalidation_keeps_the_config(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(server, errors=errors, max_stale_ms=60_000)
    lp._clock = clock
    lp.preload()
    server.config_handler = lambda call: TransportResponse(304, {}, b"")
    clock.advance(30_000)
    assert lp.get_price("pro", new_visitor()).reason == "experiment"
    wait_idle(lp)
    assert server.config_calls[-1].headers["If-None-Match"] == '"v5"'
    assert lp._fetched_at == clock.now
    clock.advance(45_000)
    assert lp._usable(clock.now) is not None
    assert errors == []


def test_unusable_config_is_fetched_without_etag(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(server, errors=errors, max_stale_ms=60_000, timeout_ms=1_000)
    lp._clock = clock
    lp.preload()
    clock.advance(60_000)
    assert lp.get_price("pro", new_visitor()).reason == "experiment"
    assert "If-None-Match" not in server.config_calls[-1].headers


def test_304_without_if_none_match_is_a_failure(server: FakeServer, errors: List[Exception]) -> None:
    server.config_handler = lambda call: TransportResponse(304, {}, b"")
    lp = make_client(server, errors=errors)
    assert lp.get_price("pro", new_visitor()).reason == "no_config"
    assert messages(errors) == ["Leverprice config: HTTP 304"]


def test_newer_config_version_triggers_unconditional_fetch(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(server, errors=errors)
    lp._clock = clock
    lp.preload()
    assert lp.get_price("basic", new_visitor()).price == 1200

    server.envelope = KEYS.envelope(config_payload(6, products=default_products(3000)))
    server.events_handler = lambda call: TransportResponse(202, {"Leverprice-Config-Version": "6"}, b"")
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=2900, currency="usd")
    wait_for(lambda: len(server.config_calls) == 2)
    wait_idle(lp)
    assert "If-None-Match" not in server.config_calls[-1].headers
    assert lp.get_price("basic", new_visitor()).price == 1500
    assert lp._server_version == 6


def test_catch_up_waits_between_attempts(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(server, errors=errors)
    lp._clock = clock
    server.config_handler = lambda call: TransportResponse(
        200, {"ETag": '"v5"', "Leverprice-Config-Version": "9"}, json.dumps(server.envelope)
    )
    lp.preload()
    assert lp._server_version == 9
    lp.get_price("basic", new_visitor())
    wait_idle(lp)
    assert len(server.config_calls) == 2
    assert "If-None-Match" not in server.config_calls[1].headers
    lp.get_price("basic", new_visitor())
    wait_idle(lp)
    assert len(server.config_calls) == 2
    clock.advance(5_000)
    lp.get_price("basic", new_visitor())
    wait_idle(lp)
    assert len(server.config_calls) == 3


def test_refresh_interval(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(server, errors=errors, refresh_interval_ms=1_000)
    lp._clock = clock
    lp.preload()
    lp.get_price("basic", new_visitor())
    assert len(server.config_calls) == 1
    clock.advance(1_000)
    lp.get_price("basic", new_visitor())
    wait_idle(lp)
    assert len(server.config_calls) == 2
    assert server.config_calls[1].headers["If-None-Match"] == '"v5"'


def test_stalled_fetch_is_replaced(server: FakeServer, errors: List[Exception]) -> None:
    clock = Clock()
    release = threading.Event()
    first = True

    def handler(call: Call) -> TransportResponse:
        nonlocal first
        if first:
            first = False
            release.wait(5)
        return server._serve_config(call)

    server.config_handler = handler
    lp = make_client(server, errors=errors, timeout_ms=50)
    lp._clock = clock
    assert lp.get_price("basic", new_visitor()).reason == "no_config"
    clock.advance(_client.STALL_MS)
    lp.refresh()
    assert len(server.config_calls) == 2
    assert lp.get_price("basic", new_visitor()).reason == "base_price"
    release.set()


def test_request_hard_timeout(server: FakeServer, errors: List[Exception]) -> None:
    release = threading.Event()
    server.config_handler = lambda call: release.wait(5) and server._serve_config(call)  # type: ignore[return-value]
    lp = make_client(server, errors=errors)
    lp._request_timeout_ms = 100
    started = time.monotonic()
    lp.refresh()
    release.set()
    assert time.monotonic() - started < 1
    assert messages(errors) == ["Leverprice: GET /v1/config timed out after 100 ms"]


def test_transport_errors_and_bad_bodies(server: FakeServer, errors: List[Exception]) -> None:
    def boom(call: Call) -> TransportResponse:
        raise OSError("connection refused")

    server.config_handler = boom
    lp = make_client(server, errors=errors)
    lp.refresh()
    server.config_handler = lambda call: TransportResponse(200, {}, b"<html>")
    lp.refresh()
    server.config_handler = lambda call: TransportResponse(200, {}, b'{"payload": 1}')
    lp.refresh()
    assert messages(errors) == [
        "Leverprice: GET /v1/config failed: connection refused",
        "Leverprice config: response is not JSON",
        "Leverprice config: response is not a signed config",
    ]
    assert lp.get_price("pro", new_visitor()).reason == "no_config"


def test_rejected_config_keeps_the_current_one(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    lp.preload()
    server.envelope = KEYS.envelope(config_payload(4))
    lp.refresh()
    server.envelope = KEYS.envelope(config_payload(7, project_id="proj_other"))
    lp.refresh()
    server.envelope = KEYS.envelope(config_payload(8, secret="pp_sk_test_" + "Z" * 40))
    lp.refresh()
    assert messages(errors) == [
        "Leverprice config: refusing to roll back from version 5 to 4",
        "Leverprice config: project changed from proj_test to proj_other; refusing",
        "Leverprice config: not issued for this API key; refusing",
    ]
    assert lp._config is not None
    assert lp._config.version == 5


def test_revoked_signing_key_makes_the_config_unusable(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors, timeout_ms=200)
    lp.preload()
    server.envelope = KEYS.envelope(config_payload(4), key_id="k2", keyset_version=2, keyset_keys=("k2",))
    lp.refresh()
    assert messages(errors) == ["Leverprice config: refusing to roll back from version 5 to 4"]
    assert lp._usable(lp._clock()) is None


def test_malformed_products_reported_once_per_version(server: FakeServer, errors: List[Exception]) -> None:
    products = {**default_products(), "broken": {"currency": "dollars"}, "half": {"currency": "usd", "price": 1.5}}
    server.envelope = KEYS.envelope(config_payload(products=products))
    lp = make_client(server, errors=errors, products={**GUARDS, "broken": {"currency": "usd", "fallback_price": 5}})
    lp.preload()
    lp.refresh()
    assert messages(errors) == ["Leverprice config v5: ignored malformed products broken, half"]
    assert lp.get_price("broken", new_visitor()).reason == "unknown_product"


def test_events_failure_requeues_money_and_drops_exposures(server: FakeServer, errors: List[Exception]) -> None:
    server.events_handler = lambda call: TransportResponse(503, {}, b"")
    lp = make_client(server, errors=errors)
    lp.preload()
    assert lp.get_price("pro", new_visitor()).exposure_logged
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=2900, currency="usd")
    assert len(server.event_calls) == 3
    assert [e["type"] for e in server.batches[0]["events"]] == ["exposure", "purchase"]
    assert [e["type"] for e in lp._queue] == ["purchase"]
    assert messages(errors) == ["Leverprice events: HTTP 503"]

    server.events_handler = lambda call: TransportResponse(202, {}, b"")
    lp.flush()
    assert [e["type"] for e in server.batches[-1]["events"]] == ["purchase"]
    assert lp._queue == []


@pytest.mark.parametrize("status", [408, 429, 500])
def test_retryable_statuses(server: FakeServer, errors: List[Exception], status: int) -> None:
    server.events_handler = lambda call: TransportResponse(status, {}, b"")
    lp = make_client(server, errors=errors)
    lp.track_refund(provider="stripe", order_id="pi_1", currency="usd", refunded_total=100)
    assert len(server.event_calls) == 3
    assert messages(errors) == [f"Leverprice events: HTTP {status}"]


def test_network_errors_are_retried(server: FakeServer, errors: List[Exception]) -> None:
    attempts = []

    def flaky(call: Call) -> TransportResponse:
        attempts.append(call)
        if len(attempts) < 3:
            raise OSError("reset")
        return TransportResponse(200, {}, b"")

    server.events_handler = flaky
    lp = make_client(server, errors=errors)
    lp.track_purchase(provider="polar", order_id="o1", amount=100, currency="eur")
    assert len(attempts) == 3
    assert errors == []
    assert lp._queue == []


def test_400_is_not_retried(server: FakeServer, errors: List[Exception]) -> None:
    server.events_handler = lambda call: TransportResponse(400, {}, b"x" * 400)
    lp = make_client(server, errors=errors)
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=2900, currency="usd")
    assert len(server.event_calls) == 1
    assert messages(errors) == ["Leverprice events rejected: HTTP 400 " + "x" * 300]
    assert lp._queue == []


def test_batches_of_500(server: FakeServer, errors: List[Exception], monkeypatch: pytest.MonkeyPatch) -> None:
    lp = make_client(server, errors=errors)
    monkeypatch.setattr(lp, "_flush_soon", lambda: None)
    for i in range(1200):
        lp._record_exposure("exp", "var", f"{i:032x}")
    lp.flush()
    assert [len(b["events"]) for b in server.batches] == [500, 500, 200]


def test_drain_sends_only_what_was_queued(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)

    def refill(call: Call) -> TransportResponse:
        if len(server.batches) == 1:
            lp._record_exposure("exp", "var", "f" * 32)
        return TransportResponse(202, {}, b"")

    server.events_handler = refill
    lp._record_exposure("exp", "var", "0" * 32)
    lp.flush()
    assert len(server.batches) == 1
    assert len(lp._queue) == 1
    lp.close()
    assert lp._queue == []


def test_queue_limit_drops_exposures_first(
    server: FakeServer, errors: List[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_client, "MAX_QUEUE", 3)
    server.events_handler = lambda call: TransportResponse(503, {}, b"")
    lp = make_client(server, errors=errors, flush_interval_ms=60_000)
    monkeypatch.setattr(lp, "flush", lambda: None)
    lp._record_exposure("exp", "var", "0" * 32)
    lp._record_exposure("exp", "var", "1" * 32)
    for order in ("o1", "o2", "o3", "o4"):
        lp.track_purchase(provider="stripe", order_id=order, amount=1, currency="usd")
    assert [e.get("orderId") for e in lp._queue] == ["o2", "o3", "o4"]
    assert messages(errors) == ["Leverprice: event queue full, dropped purchase o1"]
    lp._cancel_timer()


def test_auto_flush_at_100(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors, flush_interval_ms=60_000)
    for i in range(100):
        lp._record_exposure("exp", "var", f"{i:032x}")
    assert wait_for(lambda: len(server.events) == 100)
    assert lp._timer is None


def test_flush_timer(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors, flush_interval_ms=30)
    lp._record_exposure("exp", "var", "0" * 32)
    assert lp._timer is not None
    assert lp._timer.daemon
    assert wait_for(lambda: len(server.events) == 1)
    assert wait_for(lambda: lp._timer is None)


def test_exposures_are_deduplicated(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    first = lp.get_price("pro", new_visitor(), user_id="user_1")
    second = lp.get_price("pro", new_visitor(), user_id="user_1")
    lp.flush()
    assert first.exposure_logged
    assert second.exposure_logged
    assert first.visitor_id == second.visitor_id
    assert len(server.events) == 1


def test_manual_exposure(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    quote = lp.get_price("pro", new_visitor(), exposure="manual")
    assert not quote.exposure_logged
    assert "exp_pro" not in decode_cookie(quote.set_cookie[0])["x"]
    assert lp.log_exposure(quote) is True
    assert quote.exposure_logged
    assert lp.log_exposure(quote) is False
    lp.flush()
    assert [(e["type"], e["visitorId"]) for e in server.events] == [("exposure", quote.visitor_id)]
    fallback = lp.get_price("basic", new_visitor())
    assert lp.log_exposure(fallback) is False


def test_same_request_shares_one_visitor(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    request = Headers(new_visitor())
    pro = lp.get_price("pro", request)
    team = lp.get_price("team", request)
    assert pro.visitor_id == team.visitor_id
    assert list(decode_cookie(pro.set_cookie[0])["a"]) == ["exp_pro"]
    state = decode_cookie(team.set_cookie[0])
    assert list(state["a"]) == ["exp_team", "exp_pro"]
    assert state["x"] == ["exp_team", "exp_pro"]
    again = lp.get_price("pro", request)
    assert again.visitor_id == pro.visitor_id
    assert again.set_cookie == []


def test_wsgi_environ_shares_one_visitor(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    environ = {"REQUEST_METHOD": "GET", "wsgi.version": (1, 0), "HTTP_USER_AGENT": CHROME}
    pro = lp.get_price("pro", environ)
    team = lp.get_price("team", environ)
    assert pro.visitor_id == team.visitor_id
    plain = new_visitor()
    assert lp.get_price("pro", plain).visitor_id != lp.get_price("pro", plain).visitor_id


def test_returning_visitor_cookie_round_trip(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    first = lp.get_price("pro", new_visitor())
    cookie = first.set_cookie[0].split(";", 1)[0]
    returning = lp.get_price("pro", {"user-agent": CHROME, "cookie": f"theme=dark; {cookie}"})
    assert (returning.visitor_id, returning.variant_id, returning.set_cookie) == (
        first.visitor_id,
        first.variant_id,
        [],
    )
    legacy = lp.get_price("pro", cookie=cookie.split("=", 1)[1], user_agent=CHROME)
    assert legacy.visitor_id == first.visitor_id


def test_user_ids(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    by_str = lp.get_price("pro", new_visitor(), user_id="42").visitor_id
    assert lp.get_price("pro", new_visitor(), user_id=42).visitor_id == by_str
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    by_uuid = lp.get_price("pro", new_visitor(), user_id=uid).visitor_id
    assert by_uuid == lp.get_price("pro", new_visitor(), user_id=str(uid)).visitor_id
    anonymous = {lp.get_price("pro", new_visitor(), user_id=v).visitor_id for v in (True, "", 1.5, object())}
    assert len(anonymous) == 4


def test_explicit_user_agent_and_bot_quotes(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    crawler = lp.get_price("pro", {"user-agent": CHROME}, user_agent=GOOGLEBOT)
    assert (crawler.reason, crawler.price, crawler.set_cookie, crawler.is_fallback) == ("bot", 2400, [], False)
    server.config_handler = lambda call: TransportResponse(503, {}, b"")
    offline = make_client(server, errors=errors)
    bot = offline.get_price("pro", {"user-agent": GOOGLEBOT})
    assert (bot.reason, bot.price, bot.set_cookie, bot.is_fallback) == ("bot", 1900, [], True)
    lp.flush()
    assert server.events == []


def test_kill_switch_option(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors, disabled=True)
    quote = lp.get_price("pro", new_visitor())
    assert (quote.reason, quote.is_fallback, quote.set_cookie) == ("disabled", True, [])
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=1, currency="usd")
    lp.track_refund(provider="stripe", order_id="pi_1", currency="usd", refunded_total=1)
    lp.preload()
    lp.refresh()
    lp.flush()
    assert server.calls == []
    assert errors == []


def test_kill_switch_env(server: FakeServer, errors: List[Exception], monkeypatch: pytest.MonkeyPatch) -> None:
    lp = make_client(server, errors=errors)
    lp.preload()
    calls = len(server.calls)
    monkeypatch.setenv("LEVERPRICE_DISABLED", " TRUE ")
    assert lp.get_price("pro", new_visitor()).reason == "disabled"
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=1, currency="usd")
    assert len(server.calls) == calls
    monkeypatch.setenv("LEVERPRICE_DISABLED", "1")
    assert lp.get_price("pro", new_visitor()).reason == "disabled"
    monkeypatch.setenv("LEVERPRICE_DISABLED", "0")
    assert lp.get_price("pro", new_visitor()).reason == "experiment"
    assert errors == []


@pytest.mark.parametrize("key", ["pp_sk_live_short", "sk_live_" + "a" * 40, "pp_sk_prod_" + "a" * 40])
def test_malformed_secret_key(server: FakeServer, errors: List[Exception], key: str) -> None:
    lp = make_client(server, errors=errors, secret_key=key)
    assert len(errors) == 1
    assert str(errors[0]).startswith("Leverprice: secret_key is not a pp_sk_live_")
    assert lp.get_price("pro", new_visitor()).reason == "disabled"
    lp.track_purchase(provider="stripe", order_id="pi_1", amount=1, currency="usd")
    assert server.calls == []
    assert len(errors) == 1


def test_secret_key_edge_cases(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (None, "", "   "):
        errors: List[Exception] = []
        lp = make_client(server, errors=errors, secret_key=key)
        assert errors == []
        assert lp.get_price("pro").reason == "disabled"
    errors = []
    make_client(server, errors=errors, secret_key="bad", disabled=True)
    monkeypatch.setenv("LEVERPRICE_DISABLED", "true")
    make_client(server, errors=errors, secret_key="bad")
    assert errors == []
    monkeypatch.delenv("LEVERPRICE_DISABLED")
    assert make_client(server, secret_key=f"  {SECRET}\n").get_price("pro", new_visitor()).reason == "experiment"


def test_throwing_on_error_changes_nothing(server: FakeServer) -> None:
    def explode(err: Exception) -> None:
        raise RuntimeError("logger down")

    make_client(server, on_error=explode, secret_key="bad")
    server.config_handler = lambda call: TransportResponse(503, {}, b"")
    lp = make_client(server, on_error=explode)
    assert lp.get_price("pro", new_visitor()).reason == "no_config"
    lp.track_purchase(provider="paypal", order_id="x", amount=1, currency="usd")
    lp.track_refund(provider="stripe", order_id="x", currency="usd")


def test_default_on_error_logs_a_warning(server: FakeServer, caplog: pytest.LogCaptureFixture) -> None:
    server.config_handler = lambda call: TransportResponse(503, {}, b"")
    lp = Leverprice(SECRET, GUARDS, trusted_root_keys=KEYS.root_keys, transport=server)
    with caplog.at_level(logging.WARNING, logger="leverprice"):
        lp.refresh()
    assert [r.getMessage() for r in caplog.records] == ["Leverprice config: HTTP 503"]


def test_track_purchase_reads_attribution_and_converts_seconds(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    quote = lp.get_price("pro", new_visitor())
    lp.track_purchase(
        provider="stripe",
        order_id="pi_1",
        amount=2900.5,
        currency="USD",
        metadata={**quote.metadata, "other": "x"},
        timestamp=1_800_000_000,
    )
    purchase = server.events[-1]
    assert purchase == {
        "type": "purchase",
        "provider": "stripe",
        "orderId": "pi_1",
        "amount": 2901,
        "currency": "usd",
        "timestamp": 1_800_000_000_000,
        "attribution": {
            "experimentId": quote.experiment_id,
            "variantId": quote.variant_id,
            "visitorId": quote.visitor_id,
            "signature": quote.metadata["pp_sig"],
        },
    }
    lp.track_purchase(
        provider="lemonsqueezy",
        order_id=12345,
        amount=Decimal("1999.49"),
        currency="usd",
        metadata={**quote.metadata, "pp_sig": "forged"},
        timestamp=1_800_000_000_123,
    )
    event = server.events[-1]
    assert (event["orderId"], event["amount"], event["timestamp"]) == ("12345", 1999, 1_800_000_000_123)
    assert event["attribution"]["signature"] is None

    class StripeObject:
        def __init__(self, data: Dict[str, str]) -> None:
            self._data = data

        def get(self, key: str) -> Any:
            return self._data.get(key)

    lp.track_purchase(
        provider="stripe", order_id="pi_2", amount=1, currency="usd", metadata=StripeObject(quote.metadata)
    )  # type: ignore[arg-type]
    assert server.events[-1]["attribution"]["visitorId"] == quote.visitor_id
    when = datetime(2027, 1, 15, tzinfo=timezone.utc)
    lp.track_purchase(provider="dodo", order_id="o", amount=1, currency="usd", metadata={"pp_exp": "e"}, timestamp=when)
    assert server.events[-1]["attribution"] is None
    assert server.events[-1]["timestamp"] == int(when.timestamp() * 1000)
    assert errors == []


def test_track_refund_shapes_and_validation(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    lp.track_refund(provider="stripe", order_id="pi_1", currency="usd", refund_id="re_1", amount=500, timestamp=1)
    lp.track_refund(provider="other", order_id="pi_1", currency="usd", refunded_total=2900)
    assert server.events[0] == {
        "type": "refund",
        "provider": "stripe",
        "orderId": "pi_1",
        "currency": "usd",
        "timestamp": 1000,
        "refundId": "re_1",
        "amount": 500,
    }
    assert server.events[1]["refundedTotal"] == 2900
    assert "refundId" not in server.events[1]
    lp.track_refund(provider="stripe", order_id="pi_1", currency="usd")
    lp.track_purchase(provider="paypal", order_id="x", amount=1, currency="usd")
    lp.track_purchase(provider="stripe", order_id=None, amount="12", currency="usd")  # type: ignore[arg-type]
    assert messages(errors) == [
        "Leverprice: dropped invalid refund event: refundId must be a string of 1–255 characters",
        'Leverprice: dropped invalid purchase event: unknown provider "paypal"',
        "Leverprice: dropped invalid purchase event: orderId must be a string of 1–255 characters",
    ]
    assert len(server.events) == 2


def test_unknown_product_raises(server: FakeServer) -> None:
    lp = make_client(server, disabled=True)
    with pytest.raises(UnknownProductError, match='unknown product "nope"') as info:
        lp.get_price("nope")
    assert isinstance(info.value, KeyError)
    assert str(info.value) == 'Leverprice: unknown product "nope". Add it to the products option.'


@pytest.mark.parametrize(
    ("guard", "message"),
    [
        ({"currency": "usdx", "fallback_price": 1}, "currency must be an ISO 4217 code"),
        ({"currency": "usd", "fallback_price": 0}, "fallback_price must be a positive integer"),
        ({"currency": "usd", "fallback_price": 19.0}, "fallback_price must be a positive integer"),
        ({"currency": "usd", "fallback_price": True}, "fallback_price must be a positive integer"),
        ({"currency": "usd", "fallback_price": 5, "min_price": 9, "max_price": 3}, "min_price <= max_price"),
        ({"currency": "usd", "fallback_price": 5, "fallbackPrice": 5}, "unknown option fallbackPrice"),
        ({"fallback_price": 5}, "currency must be"),
    ],
)
def test_guard_validation(guard: Dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Leverprice(SECRET, {"pro": guard})


def test_option_validation() -> None:
    with pytest.raises(ValueError, match="cookie_name"):
        Leverprice(SECRET, {}, cookie_name="bad name")
    with pytest.raises(ValueError, match="timeout_ms"):
        Leverprice(SECRET, {}, timeout_ms=-1)
    with pytest.raises(TypeError, match="transport"):
        Leverprice(SECRET, {}, transport=object())  # type: ignore[arg-type]
    lp = Leverprice(SECRET, {"pro": ProductGuard("USD", 1900, max_price=2000)}, disabled=True)
    quote = lp.get_price("pro")
    assert (quote.currency, quote.price) == ("usd", 1900)


def test_cookie_options(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(
        server,
        errors=errors,
        cookie_name="lp",
        cookie_secure=False,
        cookie_domain=".shop.test",
        cookie_max_age_seconds=60,
    )
    quote = lp.get_price("pro", new_visitor())
    assert quote.set_cookie[0].startswith("lp=")
    assert quote.set_cookie[0].endswith("; Path=/; Max-Age=60; SameSite=Lax; HttpOnly; Domain=.shop.test")
    pinned = lp.get_price("pro", {"cookie": "lp_f=1"})
    assert pinned.set_cookie == ["lp_f=1; Path=/; Max-Age=1800; SameSite=Lax; HttpOnly; Domain=.shop.test"]


def test_fork_reset_by_pid_check(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    lp.preload()
    lp._record_exposure("exp", "var", "0" * 32)
    lp._cancel_timer()
    old_lock = lp._lock
    lp._pid = -1
    lp.flush()
    assert lp._lock is not old_lock
    assert server.events == []
    assert lp.get_price("pro", new_visitor()).reason == "experiment"


def test_context_manager_flushes(server: FakeServer, errors: List[Exception]) -> None:
    with make_client(server, errors=errors) as lp:
        lp.get_price("pro", new_visitor())
    assert len(server.events) == 1
    assert lp._timer is None
