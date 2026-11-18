from __future__ import annotations

import asyncio
import threading
import time
from typing import List

import pytest

from _support import (
    CHROME,
    COUNTRY_TEST_GUARDS,
    PIN,
    REGIONAL_GUARDS,
    Call,
    FakeServer,
    Headers,
    SigningKeys,
    config_payload,
    country_test_products,
    make_client,
    regional_products,
    wait_idle,
)
from leverprice import TransportResponse

KEYS = SigningKeys()


def test_get_price_async_matches_sync() -> None:
    server = FakeServer(KEYS.envelope(config_payload()))
    lp = make_client(server)

    async def main() -> None:
        quote = await lp.get_price_async("pro", {"user-agent": CHROME}, user_id="u1")
        same = lp.get_price("pro", {"user-agent": CHROME}, user_id="u1")
        assert quote.reason == "experiment"
        assert (quote.visitor_id, quote.variant_id, quote.metadata) == (same.visitor_id, same.variant_id, same.metadata)
        await lp.flush_async()

    asyncio.run(main())
    assert len(server.events) == 1


def test_get_price_async_serves_regional_prices() -> None:
    server = FakeServer(KEYS.envelope(config_payload(products=regional_products())))
    lp = make_client(server, products=REGIONAL_GUARDS, country_header="cf-ipcountry", local_currencies=True)

    async def main() -> None:
        request = {"user-agent": CHROME, "cf-ipcountry": "IN"}
        quote = await lp.get_price_async("geox", request, user_id="u1")
        same = lp.get_price("geox", request, user_id="u1")
        assert (quote.currency, quote.country, quote.regional, quote.checkout_url) == ("inr", "IN", True, None)
        assert (quote.price, quote.variant_id) == (same.price, same.variant_id)
        passed = await lp.get_price_async("geo", request, country="mx")
        assert (passed.price, passed.currency, passed.country, passed.regional) == (2000, "usd", "MX", True)
        await lp.flush_async()

    asyncio.run(main())
    assert [e.get("country") for e in server.events] == ["IN"]


def test_get_price_async_honours_scopes_and_splits() -> None:
    server = FakeServer(KEYS.envelope(config_payload(products=country_test_products())))
    lp = make_client(server, products=COUNTRY_TEST_GUARDS)

    async def main() -> None:
        request = {"user-agent": CHROME}
        inside = await lp.get_price_async("local", request, user_id="u1", country="IN")
        same = lp.get_price("local", request, user_id="u1", country="IN")
        assert (inside.reason, inside.variant_id, inside.visitor_id) == ("experiment", "b", same.visitor_id)
        outside = await lp.get_price_async("local", request, user_id="u1", country="US")
        assert (outside.reason, outside.price, outside.set_cookie) == ("base_price", 2900, [])
        assert (await lp.get_price_async("split", request, country="BR")).variant_id == "a"
        await lp.flush_async()

    asyncio.run(main())
    assert [(e["experimentId"], e["country"]) for e in server.events] == [("exp_local", "IN"), ("exp_split", "BR")]


def test_async_config_loaded_between_kick_and_wait_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(KEYS.envelope(config_payload()))
    lp = make_client(server)
    kick = lp._kick

    def kick_and_finish(now: int) -> None:
        kick(now)
        wait_idle(lp)

    monkeypatch.setattr(lp, "_kick", kick_and_finish)

    async def main() -> None:
        quote = await lp.get_price_async("basic", {"user-agent": CHROME})
        assert quote.reason == "base_price"

    asyncio.run(main())
    assert len(server.config_calls) == 1


def test_cold_start_does_not_block_the_loop() -> None:
    release = threading.Event()
    server = FakeServer(KEYS.envelope(config_payload()))

    def slow(call: Call) -> TransportResponse:
        release.wait(5)
        return TransportResponse(503, {}, b"")

    server.config_handler = slow
    lp = make_client(server, timeout_ms=150)
    ticks: List[float] = []

    async def ticker() -> None:
        for _ in range(10):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def main() -> None:
        started = time.monotonic()
        quotes = await asyncio.gather(*(lp.get_price_async("pro", {"user-agent": CHROME}) for _ in range(20)), ticker())
        elapsed = time.monotonic() - started
        assert all(q.reason == "no_config" and q.set_cookie == [PIN] for q in quotes[:-1])
        assert 0.12 <= elapsed < 1.0

    asyncio.run(main())
    release.set()
    assert len(ticks) == 10
    assert len(server.config_calls) == 1


def test_async_quotes_of_one_request_share_the_cookie() -> None:
    server = FakeServer(KEYS.envelope(config_payload()))
    lp = make_client(server)

    async def main() -> None:
        request = Headers({"user-agent": CHROME})
        pro, team = await asyncio.gather(lp.get_price_async("pro", request), lp.get_price_async("team", request))
        assert pro.visitor_id == team.visitor_id
        assert len(team.set_cookie) == 1

    asyncio.run(main())


def test_async_tracking_and_lifecycle() -> None:
    server = FakeServer(KEYS.envelope(config_payload()))
    errors: List[Exception] = []

    async def main() -> None:
        async with make_client(server, errors=errors) as lp:
            await lp.preload_async()
            await lp.refresh_async()
            await lp.track_purchase_async(provider="stripe", order_id="pi_1", amount=2900, currency="usd")
            await lp.track_refund_async(provider="stripe", order_id="pi_1", currency="usd", refunded_total=2900)
            assert [e["type"] for e in server.events] == ["purchase", "refund"]
            await lp.get_price_async("pro", {"user-agent": CHROME})
        assert server.events[-1]["type"] == "exposure"

    asyncio.run(main())
    assert len(server.config_calls) == 2
    assert errors == []
