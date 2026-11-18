from __future__ import annotations

import itertools
import json
import logging
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

import leverprice
from _support import (
    CHROME,
    COUNTRY_TEST_GUARDS,
    GOOGLEBOT,
    GUARDS,
    PIN,
    REGIONAL_GUARDS,
    REGIONS,
    SECRET,
    Call,
    Clock,
    FakeServer,
    Headers,
    SigningKeys,
    b64url,
    b64url_decode,
    config_payload,
    country_test_products,
    decode_cookie,
    default_products,
    make_client,
    regional_products,
    wait_for,
    wait_idle,
)
from leverprice import (
    MIN_KEYSET_VERSION,
    ROOT_PUBLIC_KEYS,
    SDK_VERSION,
    Leverprice,
    ProductGuard,
    Quote,
    TransportResponse,
    UnknownProductError,
    _client,
)
from leverprice._ed25519 import decode_point
from leverprice._wire import (
    MAX_REGIONS,
    MAX_SCOPE,
    MAX_SPLITS,
    STANDARD_LEVEL,
    ConfigExperiment,
    ConfigRegion,
    parse_config,
)

KEYS = SigningKeys()


@pytest.fixture
def server() -> FakeServer:
    return FakeServer(KEYS.envelope(config_payload()))


@pytest.fixture
def regional_server() -> FakeServer:
    return FakeServer(KEYS.envelope(config_payload(products=regional_products())))


@pytest.fixture
def country_test_server() -> FakeServer:
    return FakeServer(KEYS.envelope(config_payload(products=country_test_products())))


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
    assert SDK_VERSION == leverprice.__version__ == "0.4.1"
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
        assert call.headers["User-Agent"] == "leverprice-sdk-python/0.4.1"
        assert not any(k.lower() == "origin" for k in call.headers)
    assert "If-None-Match" not in get.headers
    assert post.headers["Content-Type"] == "application/json"
    assert json.loads(post.body or b"")["sdk"] == {"name": "leverprice-python", "version": "0.4.1"}
    assert errors == []


def test_cold_start_times_out(server: FakeServer, errors: List[Exception]) -> None:
    release = threading.Event()

    def slow(call: Call) -> TransportResponse:
        release.wait(5)
        return TransportResponse(503, {}, b"")

    server.config_handler = slow
    lp = make_client(server, errors=errors, timeout_ms=150)
    waits: List[float] = []
    quotes: List[Any] = []

    def visit(delay: float) -> None:
        time.sleep(delay)
        started = time.monotonic()
        quotes.append(lp.get_price("pro", new_visitor()))
        waits.append(time.monotonic() - started)

    threads = [threading.Thread(target=visit, args=(d,)) for d in (0, 0.04, 0.08)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(q.reason == "no_config" and q.set_cookie == [PIN] and q.price == 1900 for q in quotes)
    assert all(0.12 <= w < 1.0 for w in waits), waits
    assert len(server.config_calls) == 1
    assert messages(errors) == [
        "Leverprice config: still loading after 150 ms (timeout_ms); serving fallback prices until it arrives"
    ]
    release.set()


def test_cold_start_wait_is_not_shortened_by_preload(server: FakeServer, errors: List[Exception]) -> None:
    release = threading.Event()

    def held(call: Call) -> TransportResponse:
        release.wait(5)
        return server._serve_config(call)

    server.config_handler = held
    lp = make_client(server, errors=errors, timeout_ms=400)
    threading.Thread(target=lp.preload, daemon=True).start()  # started at boot, not awaited
    time.sleep(0.25)
    # The config arrives 450 ms after preload() started, 200 ms into this request's own wait.
    threading.Timer(0.2, release.set).start()
    quote = lp.get_price("pro", new_visitor())
    assert quote.reason != "no_config"
    assert not quote.is_fallback
    assert errors == []
    assert len(server.config_calls) == 1


def test_config_loaded_between_kick_and_wait_is_served(
    server: FakeServer, errors: List[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    lp = make_client(server, errors=errors)
    kick = lp._kick

    def kick_and_finish(now: int) -> None:
        kick(now)
        wait_idle(lp)

    monkeypatch.setattr(lp, "_kick", kick_and_finish)
    assert lp.get_price("basic", new_visitor()).reason == "base_price"
    assert len(server.config_calls) == 1
    assert errors == []


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


def test_malformed_regions_drop_the_product(errors: List[Exception]) -> None:
    base = {"id": "prod", "currency": "usd", "price": 2900, "minPrice": 1900, "maxPrice": 4900}
    codes = ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]
    many = [{"countries": [code], "currency": "usd", "price": 2000} for code in codes[: MAX_REGIONS + 1]]

    def region(**fields: Any) -> List[Dict[str, Any]]:
        return [{"countries": ["IN"], "currency": "inr", "price": 99_900, **fields}]

    def experiment(*regional: Any) -> Dict[str, Any]:
        variants: List[Dict[str, Any]] = []
        for index, prices in enumerate(regional):
            variant = {"id": f"var_{index}", "price": 1900, "weight": 5000}
            variants.append(variant if prices is None else {**variant, "regional": prices})
        return {"id": "exp_1", "salt": "s", "variants": variants}

    products = {
        "flat": {**base, "regions": []},
        "none": {**base, "regions": None},
        "geo": {**base, "regions": REGIONS},
        "test": {**base, "regions": REGIONS, "experiment": experiment([1300, 64_900], [2700, 129_900])},
        "ignored": {**base, "experiment": experiment([1], "junk")},
        "most": {**base, "regions": many[:MAX_REGIONS]},
        "many": {**base, "regions": many},
        "twice": {**base, "regions": [*REGIONS, *region(currency="usd")]},
        "repeat": {**base, "regions": region(countries=["IN", "IN"])},
        "lower": {**base, "regions": region(countries=["in"])},
        "empty": {**base, "regions": region(countries=[])},
        "free": {**base, "regions": region(price=0)},
        "half": {**base, "regions": region(price=1.5)},
        "flag": {**base, "regions": region(price=True)},
        "money": {**base, "regions": region(currency="rupee")},
        "shape": {**base, "regions": {"IN": 1}},
        "short": {**base, "regions": REGIONS, "experiment": experiment([1300], [2700, 129_900])},
        "missing": {**base, "regions": REGIONS, "experiment": experiment([1300, 64_900], None)},
        "zero": {**base, "regions": REGIONS, "experiment": experiment([1300, 0], [2700, 129_900])},
    }
    lp = make_client(FakeServer(KEYS.envelope(config_payload(products=products))), errors=errors)
    lp.preload()
    assert messages(errors) == [
        "Leverprice config v5: ignored malformed products "
        "many, twice, repeat, lower, empty, free, half, flag, money, shape, short, missing, zero"
    ]
    assert lp._config is not None
    parsed = lp._config.products

    def regional(key: str) -> List[Any]:
        running = parsed[key].experiment
        assert running is not None
        return [variant.regional for variant in running.variants]

    assert (parsed["flat"].regions, parsed["flat"].region_of) == ((), {})
    assert (parsed["none"].regions, parsed["none"].region_of) == ((), {})
    # No level in the config: it compares as the standard price, so it never holds a price.
    assert parsed["geo"].regions[1] == ConfigRegion(("IN",), "inr", 99_900, STANDARD_LEVEL)
    assert parsed["geo"].region_of == {"BR": 0, "MX": 0, "IN": 1}
    assert len(parsed["most"].regions) == MAX_REGIONS
    assert regional("test") == [(1300, 64_900), (2700, 129_900)]
    assert regional("ignored") == [(), ()]


def test_malformed_scopes_and_splits_drop_the_product(errors: List[Exception]) -> None:
    base = {"currency": "usd", "price": 2900, "minPrice": 1900, "maxPrice": 4900}
    variants = [{"id": "a", "price": 1900, "weight": 5000}, {"id": "b", "price": 3900, "weight": 5000}]
    test = {"id": "exp_1", "salt": "s", "variants": variants}
    codes = ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]
    singles = [{"countries": [code], "weights": [1, 2]} for code in codes[: MAX_SPLITS + 1]]

    def scoped(countries: Any, **fields: Any) -> Dict[str, Any]:
        return {**base, "scopedExperiment": {**test, "countries": countries, **fields}}

    def split(*splits: Any) -> Dict[str, Any]:
        return {**base, "experiment": {**test, "splits": list(splits)}}

    def india(*weights: Any) -> Dict[str, Any]:
        return {"countries": ["IN"], "weights": list(weights)}

    kept = {
        "open": {**base, "experiment": {**test, "splits": None, "countries": []}, "scopedExperiment": None},
        "local": scoped(["IN", "BR"], splits=[india(0, 10_000)]),
        "widest": scoped(codes[:MAX_SCOPE]),
        "finest": split(*singles[:MAX_SPLITS]),
        "unsplit": split(),
    }
    dropped = {
        "both": {**scoped(["IN"]), "experiment": test},
        "unscoped": {**base, "scopedExperiment": test},
        "nowhere": scoped([]),
        "wider": scoped(codes[: MAX_SCOPE + 1]),
        "again": scoped(["IN", "BR", "IN"]),
        "lower": scoped(["in"]),
        "name": scoped(["IN", "Brazil"]),
        "newline": scoped(["IN\n"]),
        "number": scoped(["IN", 91]),
        "text": scoped("IN"),
        "weightless": scoped(["IN"], variants=[{"id": "a", "price": 1900}]),
        "finer": split(*singles),
        "short": split(india(10_000)),
        "long": split(india(1, 2, 3)),
        "negative": split(india(10_001, -1)),
        "half": split(india(0.5, 1)),
        "flag": split(india(True, 1)),
        "twice": split(india(1, 2), {"countries": ["BR", "IN"], "weights": [2, 1]}),
        "repeat": split({"countries": ["IN", "IN"], "weights": [1, 2]}),
        "empty": split({"countries": [], "weights": [1, 2]}),
        "code": split({"countries": ["in"], "weights": [1, 2]}),
        "shape": {**base, "experiment": {**test, "splits": {"IN": [1, 2]}}},
        "entry": split(["IN"]),
    }
    lp = make_client(FakeServer(KEYS.envelope(config_payload(products={**kept, **dropped}))), errors=errors)
    lp.preload()
    assert messages(errors) == ["Leverprice config v5: ignored malformed products " + ", ".join(dropped)]
    assert lp._config is not None
    parsed = lp._config.products
    assert list(parsed) == list(kept)

    def running(key: str) -> ConfigExperiment:
        experiment = parsed[key].experiment
        assert experiment is not None
        return experiment

    assert (running("open").scope, running("open").split_of) == (None, {})
    assert (running("local").scope, running("local").split_of) == (frozenset(("BR", "IN")), {"IN": (0, 10_000)})
    assert running("widest").scope == frozenset(codes[:MAX_SCOPE])
    assert running("finest").split_of == dict.fromkeys(codes[:MAX_SPLITS], (1, 2))
    assert running("unsplit").split_of == {}


def test_config_country_header_is_kept_only_when_it_is_a_header_name() -> None:
    def parsed(**fields: Any) -> Optional[str]:
        config, _ = parse_config(b64url(json.dumps({**config_payload(), **fields}).encode()))
        return config.country_header

    assert parsed() is None
    assert parsed(countryHeader="X-Geo-Country") == "x-geo-country"
    for value in ("x-geo-country\n", " cf-ipcountry", "cf ipcountry", "\u212aey", "", None, 42, ["cf-ipcountry"]):
        assert parsed(countryHeader=value) is None, value
    for value in ("Accept-Language", "x-forwarded-for", "X-Real-IP", "sec-ch-ua", "user-agent", "forwarded"):
        assert parsed(countryHeader=value) is None, value


def test_country_hold_and_region_levels_never_reject_a_config() -> None:
    def hold(value: Any) -> int:
        config, _ = parse_config(b64url(json.dumps({**config_payload(), "countryHold": value}).encode()))
        return config.country_hold

    assert hold(604_800) == 604_800
    assert hold(7_776_000.0) == 7_776_000
    for value in (0, -1, 7_776_001, 1.5, "604800", True, None, [1], 2**53):
        assert hold(value) == 0, value
    assert parse_config(b64url(json.dumps(config_payload()).encode()))[0].country_hold == 0

    levels = {"IN": 4138, "CH": 12_069.0, "BR": 0, "MX": -5, "AR": 1.5, "CL": "9000", "PE": True, "UY": None}
    regions = [
        {"countries": [code], "currency": "usd", "price": 2000, "level": level} for code, level in levels.items()
    ]
    regions.append({"countries": ["CO"], "currency": "usd", "price": 2000})
    product = {"id": "prod", "currency": "usd", "price": 2900, "minPrice": 1000, "maxPrice": 4900, "regions": regions}
    config, dropped = parse_config(b64url(json.dumps(config_payload(products={"geo": product})).encode()))
    assert dropped == []
    assert [region.level for region in config.products["geo"].regions] == [4138, 12_069] + [STANDARD_LEVEL] * 7


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


def test_manual_exposure_sends_the_quote_country(regional_server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(regional_server, errors=errors, products=REGIONAL_GUARDS, local_currencies=True)
    quote = lp.get_price("geox", new_visitor(), exposure="manual", country="in")
    unknown = lp.get_price("geox", new_visitor(), exposure="manual")
    assert (quote.country, quote.regional, quote.exposure_logged) == ("IN", True, False)
    assert (unknown.country, unknown.regional, unknown.exposure_logged) == (None, False, False)
    assert lp.log_exposure(quote) is True
    assert lp.log_exposure(unknown) is True
    lp.flush()
    sent = [{k: v for k, v in e.items() if k != "timestamp"} for e in regional_server.events]
    exposure = {"type": "exposure", "experimentId": "exp_geo"}
    assert sent == [
        {**exposure, "variantId": quote.variant_id, "visitorId": quote.visitor_id, "country": "IN"},
        {**exposure, "variantId": unknown.variant_id, "visitorId": unknown.visitor_id},
    ]
    assert errors == []


def test_manual_exposure_is_logged_without_a_country_that_is_not_one(
    server: FakeServer, errors: List[Exception]
) -> None:
    lp = make_client(server, errors=errors)

    def quote(visitor_id: str, country: Any) -> Quote:
        return Quote("pro", 1900, "usd", False, "experiment", "exp_1", "var_1", visitor_id, country=country)

    for visitor_id, country in (("v1", " br "), ("v2", "\u00df"), ("v3", 42), ("v4", None)):
        assert lp.log_exposure(quote(visitor_id, country)) is True
    stored = SimpleNamespace(
        reason="experiment", exposure_logged=False, experiment_id="exp_1", variant_id="var_1", visitor_id="v5"
    )
    assert lp.log_exposure(stored) is True  # type: ignore[arg-type]
    lp.flush()
    assert [(e["visitorId"], e.get("country", "none")) for e in server.events] == [
        ("v1", "BR"),
        ("v2", "none"),
        ("v3", "none"),
        ("v4", "none"),
        ("v5", "none"),
    ]
    assert errors == []


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


def test_regional_price_with_dataclass_guards(regional_server: FakeServer, errors: List[Exception]) -> None:
    guard = ProductGuard("USD", 2900)
    lp = make_client(
        regional_server, errors=errors, products={"geo": guard}, country_header=" CF-IPCountry ", local_currencies=True
    )
    india = lp.get_price("geo", {"user-agent": CHROME, "cf-ipcountry": "in"})
    assert (india.price, india.currency, india.country, india.regional) == (99_900, "inr", "IN", True)
    assert (india.reason, india.is_fallback, india.checkout_url) == ("base_price", False, None)
    elsewhere = lp.get_price("geo", {"user-agent": CHROME, "cf-ipcountry": "IN"}, country="US")
    assert (elsewhere.price, elsewhere.currency, elsewhere.country, elsewhere.regional) == (2900, "usd", "US", False)
    assert elsewhere.checkout_url == "https://buy.example.com/geo"
    assert errors == []


def test_regional_prices_in_another_currency_need_local_currencies(
    regional_server: FakeServer, errors: List[Exception]
) -> None:
    usd = {"currency": "usd", "fallback_price": 2900}
    fixed = make_client(regional_server, errors=errors, products={"geo": usd})
    local = make_client(regional_server, errors=errors, products={"geo": usd}, local_currencies=True)
    loose = make_client(regional_server, errors=errors, products={"geo": usd}, local_currencies="true")

    def served(lp: Leverprice, country: str) -> Tuple[int, str, bool]:
        quote = lp.get_price("geo", new_visitor(), country=country)
        return quote.price, quote.currency, quote.regional

    assert served(fixed, "IN") == served(loose, "IN") == (2900, "usd", False)
    assert served(local, "IN") == (99_900, "inr", True)
    assert served(fixed, "BR") == served(local, "BR") == (2000, "usd", True)
    assert errors == []


def test_min_and_max_price_from_0_3_are_accepted_and_ignored(errors: List[Exception]) -> None:
    server = FakeServer(KEYS.envelope(config_payload(products={**default_products(), **regional_products()})))
    guards: Dict[str, Dict[str, Any]] = {
        "pro": {"currency": "usd", "fallback_price": 1900, "min_price": 3000, "max_price": 3500},
        "team": {"currency": "usd", "fallback_price": 4900, "min_price": 9000, "max_price": 100},
        "basic": {"currency": "usd", "fallback_price": 1000, "min_price": "5", "max_price": None},
        "geo": {"currency": "usd", "fallback_price": 2900, "min_price": 2500, "max_price": 4900},
    }
    for products in (guards, {key: ProductGuard(**guard) for key, guard in guards.items()}):
        lp = make_client(server, errors=errors, products=products)
        pro, team = lp.get_price("pro", new_visitor()), lp.get_price("team", new_visitor())
        assert (pro.reason, pro.variant_id, pro.price) in {("experiment", "a", 1900), ("experiment", "b", 2900)}
        assert (team.reason, team.variant_id, team.price) in {("experiment", "c", 4900), ("experiment", "d", 5900)}
        basic, geo = lp.get_price("basic", new_visitor()), lp.get_price("geo", new_visitor(), country="BR")
        assert (basic.reason, basic.price) == ("base_price", 1200)
        assert (geo.reason, geo.price, geo.currency, geo.regional) == ("base_price", 2000, "usd", True)
        crawler = lp.get_price("pro", {"user-agent": GOOGLEBOT})
        assert (crawler.reason, crawler.price, crawler.is_fallback) == ("bot", 2400, False)
    assert errors == []


def test_country_header_option_wins_over_the_project_header(errors: List[Exception]) -> None:
    payload = {**config_payload(products=regional_products()), "countryHeader": "X-Geo-Country"}
    server = FakeServer(KEYS.envelope(payload))
    project = make_client(server, errors=errors, products=REGIONAL_GUARDS, local_currencies=True)
    own = make_client(server, errors=errors, products=REGIONAL_GUARDS, country_header="CF-IPCountry")
    both = {"user-agent": CHROME, "cf-ipcountry": "BR", "x-geo-country": "in"}

    def priced(lp: Leverprice, request: Any, **options: Any) -> Tuple[Optional[str], int, str]:
        quote = lp.get_price("geo", request, **options)
        return quote.country, quote.price, quote.currency

    assert priced(project, both) == ("IN", 99_900, "inr")
    assert priced(own, both) == ("BR", 2000, "usd")
    assert priced(own, {"user-agent": CHROME, "x-geo-country": "IN"}) == (None, 2900, "usd")
    assert priced(own, both, country="MX") == ("MX", 2000, "usd")
    assert priced(project, both, country="xx") == (None, 2900, "usd")
    environ = {"REQUEST_METHOD": "GET", "wsgi.version": (1, 0), "HTTP_USER_AGENT": CHROME, "HTTP_X_GEO_COUNTRY": "IN"}
    assert priced(project, environ) == ("IN", 99_900, "inr")
    assert priced(project, {**both, "user-agent": GOOGLEBOT}) == (None, 2900, "usd")
    assert errors == []


def test_scoped_test_leaves_visitors_outside_its_scope_alone(
    country_test_server: FakeServer, errors: List[Exception]
) -> None:
    lp = make_client(country_test_server, errors=errors, products=COUNTRY_TEST_GUARDS)
    inside = lp.get_price("local", new_visitor(), country="BR")
    assert (inside.reason, inside.variant_id, inside.price) == ("experiment", "a", 1900)
    returning = {"user-agent": CHROME, "cookie": inside.set_cookie[0].split(";", 1)[0]}
    for country in ("US", "Brazil", None):
        outside = lp.get_price("local", returning, country=country)
        assert (outside.reason, outside.price, outside.visitor_id) == ("base_price", 2900, None)
        assert (outside.set_cookie, outside.exposure_logged) == ([], False)
    assert lp.get_price("local", new_visitor(), country="US").set_cookie == []
    back = lp.get_price("local", returning, country="BR")
    assert (back.variant_id, back.visitor_id, back.set_cookie) == ("a", inside.visitor_id, [])
    lp.flush()
    assert [e["country"] for e in country_test_server.events] == ["BR"]
    assert errors == []


def test_split_replaces_the_weights(country_test_server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(country_test_server, errors=errors, products=COUNTRY_TEST_GUARDS)

    def variant(product: str, country: Optional[str]) -> Optional[str]:
        return lp.get_price(product, new_visitor(), country=country).variant_id

    assert [variant("split", country) for country in ("IN", "BR", None)] == ["b", "a", "a"]
    assert [variant("local", country) for country in ("IN", "BR", None)] == ["b", "a", None]
    assert errors == []


def test_sticky_assignment_beats_a_split(country_test_server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(country_test_server, errors=errors, products=COUNTRY_TEST_GUARDS)
    first = lp.get_price("split", new_visitor(), country="BR")
    returning = {"user-agent": CHROME, "cookie": first.set_cookie[0].split(";", 1)[0]}
    moved = lp.get_price("split", returning, country="IN")
    assert (first.variant_id, moved.variant_id) == ("a", "a")
    assert (moved.visitor_id, moved.set_cookie) == (first.visitor_id, [])
    assert errors == []


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

    class StripeObjectV15:
        def __init__(self, data: Dict[str, str]) -> None:
            self._data = data

        def __getattr__(self, key: str) -> Any:
            try:
                return self._data[key]
            except KeyError:
                raise AttributeError(key) from None

        def __getitem__(self, key: str) -> Any:
            return self._data[key]

        def to_dict(self) -> Dict[str, str]:
            return dict(self._data)

    lp.track_purchase(
        provider="stripe", order_id="pi_3", amount=1, currency="usd", metadata=StripeObjectV15(quote.metadata)
    )  # type: ignore[arg-type]
    assert server.events[-1]["attribution"]["signature"] == quote.metadata["pp_sig"]
    lp.track_purchase(provider="stripe", order_id="pi_4", amount=1, currency="usd", metadata=["pp_exp"])  # type: ignore[arg-type]
    assert server.events[-1]["attribution"] is None
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
        ({"currency": "usd", "fallback_price": 5, "fallbackPrice": 5}, "unknown option fallbackPrice"),
        ({"fallback_price": 5}, "currency must be"),
    ],
)
def test_guard_validation(guard: Dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Leverprice(SECRET, {"pro": guard})


def test_country_header_is_validated_before_it_is_lowercased() -> None:
    for header in ("bad name", "x:y", "\u212aey", "\u00a0cf-ipcountry", "cf-ipcountry\u2003", 5):
        with pytest.raises(ValueError, match="country_header"):
            Leverprice(SECRET, {}, country_header=header)  # type: ignore[arg-type]
    accepted = {
        "CF-IPCountry": "cf-ipcountry",
        " cf-ipcountry\n": "cf-ipcountry",
        "\tX-Country\r\n": "x-country",
        "": None,
        "  ": None,
    }
    for header, name in accepted.items():
        assert Leverprice(SECRET, {}, country_header=header)._country_header == name
    assert Leverprice(SECRET, {})._country_header is None


@pytest.mark.parametrize("header", ["Accept-Language", "x-forwarded-for", "X-Real-IP", "sec-ch-ua", "user-agent"])
def test_country_header_that_visitors_send_is_ignored_and_reported_once(errors: List[Exception], header: str) -> None:
    payload = {**config_payload(products=regional_products()), "countryHeader": "X-Geo-Country"}
    server = FakeServer(KEYS.envelope(payload))
    lp = make_client(server, errors=errors, products=REGIONAL_GUARDS, country_header=header)
    assert messages(errors) == [
        f'Leverprice: country_header "{header.lower()}" is sent by visitors\' browsers, so it would let them pick '
        "their own price; it is ignored. Name the header your CDN or host sets, such as cf-ipcountry."
    ]
    assert lp._country_header is None
    chosen = lp.get_price("geo", {header: "MX"}, user_agent=CHROME)
    assert (chosen.country, chosen.price, chosen.regional) == (None, 2900, False)
    # Ignored as if it were not set: the project's header is read.
    project = lp.get_price("geo", {header: "MX", "x-geo-country": "BR"}, user_agent=CHROME)
    assert (project.country, project.price, project.regional) == ("BR", 2000, True)
    assert len(errors) == 1


def test_country_headers_set_by_hosts_and_disabled_clients_report_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: List[Exception] = []
    for header in ("cf-ipcountry", "x-vercel-ip-country", "CloudFront-Viewer-Country", "x-geo-country", "x-forwarded"):
        assert Leverprice(SECRET, {}, country_header=header, on_error=errors.append)._country_header == header.lower()
    Leverprice(SECRET, {}, country_header="accept-language", on_error=errors.append, disabled=True)
    monkeypatch.setenv("LEVERPRICE_DISABLED", "1")
    Leverprice(SECRET, {}, country_header="accept-language", on_error=errors.append)
    assert errors == []
    monkeypatch.delenv("LEVERPRICE_DISABLED")
    Leverprice(None, {}, country_header="accept-language", on_error=errors.append)
    assert len(errors) == 1


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


def test_products_are_named_on_every_config_request(server: FakeServer, errors: List[Exception]) -> None:
    products = {
        "team": {"currency": "EUR", "fallback_price": 4900},
        "pro plan": {"currency": "usd", "fallback_price": 1900},
        "\u00e9t\u00e9": {"currency": "usd", "fallback_price": 900},
    }
    lp = make_client(server, errors=errors, products=products)
    lp.refresh()
    lp.refresh()
    server.config_handler = lambda call: TransportResponse(503, {}, b"")
    lp.refresh()
    lp.refresh()
    assert len(server.config_calls) == 4
    for call in server.config_calls:
        assert call.headers["Leverprice-Products"] == "%C3%A9t%C3%A9=usd, pro%20plan=usd, team=eur"
        assert "Leverprice-Config-Error" not in call.headers  # an outage is not a refused config
    assert make_client(server, products={})._products_header == ""


def test_a_refused_config_is_reported_until_one_is_accepted(server: FakeServer, errors: List[Exception]) -> None:
    valid = json.dumps(server.envelope)
    responses = [
        TransportResponse(200, {}, b"not json"),
        TransportResponse(200, {}, b'{"payload": "x"}'),
        TransportResponse(503, {}, b""),
        TransportResponse(200, {"ETag": '"v5"'}, valid),
        TransportResponse(200, {}, json.dumps(KEYS.envelope(config_payload(4)))),
        TransportResponse(304, {}, b""),
        TransportResponse(200, {}, valid),
        TransportResponse(200, {}, valid),
    ]
    server.config_handler = lambda call: responses.pop(0)
    lp = make_client(server, errors=errors)
    for _ in range(8):
        lp.refresh()
    assert [call.headers.get("Leverprice-Config-Error") for call in server.config_calls] == [
        None,
        "malformed",
        "malformed",
        "malformed",
        None,
        "rollback",
        "rollback",
        None,
    ]
    assert "If-None-Match" in server.config_calls[5].headers
    assert lp._config_error is None


def cookieless(form: str) -> Any:
    if form == "headers":
        return {"user-agent": CHROME, "accept-language": "en", "x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    if form == "environ":
        return {"REQUEST_METHOD": "GET", "HTTP_USER_AGENT": CHROME, "REMOTE_ADDR": "203.0.113.7"}
    if form == "asgi":
        return {"type": "http", "headers": [(b"user-agent", CHROME.encode())], "client": ("2001:db8:1:2::7", 443)}
    return SimpleNamespace(headers={"User-Agent": CHROME}, client=SimpleNamespace(host="203.0.113.7"))


@pytest.mark.parametrize("form", ["headers", "environ", "asgi", "object"])
def test_a_visitor_without_cookies_but_with_an_address_is_one_visitor(
    server: FakeServer, errors: List[Exception], form: str
) -> None:
    lp = make_client(server, errors=errors)
    quotes = [lp.get_price("pro", cookieless(form)) for _ in range(20)]
    lp.flush()
    assert len({q.visitor_id for q in quotes}) == 1
    assert len({q.variant_id for q in quotes}) == 1
    assert len(server.events) == 1
    assert errors == []


def test_a_visitor_without_cookies_or_an_address_is_new_every_time(server: FakeServer, errors: List[Exception]) -> None:
    lp = make_client(server, errors=errors)
    quotes = [lp.get_price("pro", {"user-agent": CHROME, "accept-language": "en"}) for _ in range(20)]
    lp.flush()
    assert len({q.visitor_id for q in quotes}) == 20
    assert len(server.events) == 20
    elsewhere = lp.get_price("pro", {**cookieless("headers"), "x-forwarded-for": "203.0.113.8"})
    other_browser = lp.get_price("pro", {**cookieless("headers"), "user-agent": CHROME + " Edg/142"})
    assert (
        len({lp.get_price("pro", cookieless("headers")).visitor_id, elsewhere.visitor_id, other_browser.visitor_id})
        == 3
    )


HOLD_GUARDS: Dict[str, Dict[str, Any]] = {"geo": {"currency": "usd", "fallback_price": 2900}}


def hold_server(hold: int = 604_800) -> FakeServer:
    regions = [{"countries": ["IN"], "currency": "usd", "price": 1200, "level": 4138}]
    product = {
        "id": "prod_geo",
        "currency": "usd",
        "price": 2900,
        "minPrice": 1000,
        "maxPrice": 4900,
        "regions": regions,
    }
    return FakeServer(KEYS.envelope({**config_payload(products={"geo": product}), "countryHold": hold}))


def test_vpn_guard_keeps_a_dearer_country_until_the_hold_runs_out(errors: List[Exception]) -> None:
    clock = Clock()
    lp = make_client(hold_server(), errors=errors, products=HOLD_GUARDS)
    lp._clock = clock
    home = lp.get_price("geo", new_visitor(), country="US")
    assert (home.price, home.country, home.regional) == (2900, "US", False)
    state = decode_cookie(home.set_cookie[0])
    assert (state["c"], state["t"]) == ("US", clock.now // 1000)
    cookie = {"user-agent": CHROME, "cookie": home.set_cookie[0].split(";", 1)[0]}
    clock.advance(3_600_000)
    away = lp.get_price("geo", cookie, country="IN")
    assert (away.price, away.country, away.regional, away.set_cookie) == (2900, "US", False, [])
    crawler = lp.get_price("geo", {**cookie, "user-agent": GOOGLEBOT}, country="IN")
    assert (crawler.reason, crawler.country, crawler.set_cookie) == ("bot", None, [])
    clock.advance(604_800_000)
    later = lp.get_price("geo", cookie, country="IN")
    assert (later.price, later.country, later.regional) == (1200, "IN", True)
    assert decode_cookie(later.set_cookie[0])["c"] == "IN"
    assert errors == []


def test_vpn_guard_is_off_without_a_hold(errors: List[Exception]) -> None:
    lp = make_client(hold_server(0), errors=errors, products=HOLD_GUARDS)
    home = lp.get_price("geo", new_visitor(), country="US")
    assert (home.price, home.country, home.set_cookie) == (2900, "US", [])
    assert errors == []
