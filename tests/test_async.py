from __future__ import annotations

import asyncio
import threading
import time
from typing import List

from _support import CHROME, PIN, Call, FakeServer, Headers, SigningKeys, config_payload, make_client
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
