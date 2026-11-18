from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import json
import logging
import os
import re
import secrets
import threading
import uuid
import weakref
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Dict, Generator, List, Mapping, NamedTuple, Optional, Set, Tuple, Union

from ._bots import is_bot
from ._cookie import (
    PIN_MAX_AGE_SECONDS,
    AssignmentState,
    assign,
    mark_exposed,
    set_cookie_header,
    sign_assignment,
    verify_assignment,
)
from ._request import RequestInfo, header_source, is_wsgi_environ, normalize_country, read_header, read_request, unwrap
from ._transport import Transport, UrllibTransport
from ._trust import SigningKey, TrustChain, accept_config, key_fingerprint
from ._util import (
    ASCII_WHITESPACE,
    MAX_SAFE_INTEGER,
    LeverpriceError,
    from_b64url,
    hmac_hex,
    is_id,
    js_round,
    js_trim,
    json_loads,
    now_ms,
    parse_header_int,
    safe_int,
    sha256,
)
from ._version import SDK_VERSION
from ._wire import (
    CONFIG_VERSION_HEADER,
    MAX_EVENTS_PER_REQUEST,
    Config,
    ConfigExperiment,
    ConfigProduct,
    ConfigVariant,
    event_problem,
)

ROOT_PUBLIC_KEYS: Mapping[str, str] = MappingProxyType({"r1": "5FmNzXvPMPZEuDnFT_fFd_VVNsUrWwWTKLerMWbSdcg"})
MIN_KEYSET_VERSION = 1

DEFAULT_API_URL = "https://api.leverprice.com"
SDK_NAME = "leverprice-python"
USER_AGENT = f"leverprice-sdk-python/{SDK_VERSION}"

REQUEST_TIMEOUT_MS = 10_000
STALL_MS = REQUEST_TIMEOUT_MS + 5_000
MAX_QUEUE = 5_000
AUTO_FLUSH_AT = 100
SEND_ATTEMPTS = 3
RETRY_DELAYS_S = (0.25, 1.0)
MAX_BACKOFF_MS = 30_000
CATCH_UP_GAP_MS = 5_000
MAX_DEDUPE = 10_000
EXIT_FLUSH_TIMEOUT_MS = 2_000

_KEY_RE = re.compile(r"pp_sk_(live|test)_[0-9A-Za-z]{40}")
_COOKIE_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_NAME_RE = _COOKIE_NAME_RE
_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
_SIG_RE = re.compile(r"[0-9a-f]{16}")
_GUARD_FIELDS = ("currency", "fallback_price")
_DEPRECATED_GUARD_FIELDS = ("min_price", "max_price")

_logger = logging.getLogger("leverprice")

Event = Dict[str, Any]
QuoteSteps = Generator[None, Optional[Config], "Quote"]


@dataclass(frozen=True)
class ProductGuard:
    """One product's currency and fallback price, in minor units.

    ``min_price`` and ``max_price`` are deprecated, ignored since 0.4: the price range is set in Leverprice.
    """

    currency: str
    fallback_price: int
    min_price: Optional[int] = None
    """Deprecated. Ignored since 0.4: the price range is set in Leverprice."""
    max_price: Optional[int] = None
    """Deprecated. Ignored since 0.4: the price range is set in Leverprice."""


@dataclass
class Quote:
    """The price to show and charge for one product; ``reason`` says where it came from."""

    product_key: str
    price: int
    currency: str
    is_fallback: bool
    reason: str
    experiment_id: Optional[str] = None
    variant_id: Optional[str] = None
    visitor_id: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    set_cookie: List[str] = field(default_factory=list)
    checkout_url: Optional[str] = None
    exposure_logged: bool = False
    country: Optional[str] = None
    regional: bool = False


class UnknownProductError(LeverpriceError, KeyError):
    """Raised by ``get_price`` for a product key missing from the ``products`` option."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class _Guard(NamedTuple):
    currency: str
    fallback_price: int


@dataclass(frozen=True)
class _Region:
    index: int
    currency: str
    price: int


class _Response(NamedTuple):
    status: int
    headers: Dict[str, str]
    text: str


class _ScopeCookie(NamedTuple):
    input: Optional[str]
    user_id: Optional[str]
    assignment_key: str
    state: AssignmentState


class _Context(NamedTuple):
    request: Any
    cookie: Any
    user_agent: Any
    user_id: Any
    auto_exposure: bool
    bot: Any
    country: Any


class _Scope:
    __slots__ = ("_async_lock", "cookie", "lock", "pinned")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cookie: Optional[_ScopeCookie] = None
        self.pinned = False
        self._async_lock: Optional[asyncio.Lock] = None

    def async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock


class _ScopeMap:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[int, Tuple[weakref.ref[Any], _Scope]] = {}

    def get(self, obj: Any) -> Optional[_Scope]:
        key = id(obj)
        with self._lock:
            item = self._items.get(key)
            if item is not None and item[0]() is obj:
                return item[1]
            try:
                ref = weakref.ref(obj, functools.partial(_ScopeMap._forget, self._items, key))
            except TypeError:
                return None
            scope = _Scope()
            self._items[key] = (ref, scope)
            return scope

    @staticmethod
    def _forget(items: Dict[int, Tuple[weakref.ref[Any], _Scope]], key: int, ref: weakref.ref[Any]) -> None:
        item = items.get(key)
        if item is not None and item[0] is ref:
            items.pop(key, None)


class _Flight:
    __slots__ = ("_lock", "_waiters", "done", "started_at")

    def __init__(self, started_at: int) -> None:
        self.started_at = started_at
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._waiters: List[Tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def finish(self) -> None:
        with self._lock:
            self.done.set()
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolve, future)

    async def wait_async(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            if self.done.is_set():
                return
            self._waiters.append((loop, future))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(future, seconds)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


_clients: weakref.WeakSet[Leverprice] = weakref.WeakSet()


class Leverprice:
    """Server-side Leverprice client. Create one per process and share it across threads."""

    def __init__(
        self,
        secret_key: Optional[str],
        products: Mapping[str, Union[ProductGuard, Mapping[str, Any]]],
        *,
        disabled: bool = False,
        api_url: str = DEFAULT_API_URL,
        timeout_ms: float = 500,
        refresh_interval_ms: float = 30_000,
        max_stale_ms: float = 86_400_000,
        cookie_name: str = "__pp",
        cookie_max_age_seconds: int = 15_552_000,
        cookie_secure: bool = True,
        cookie_domain: Optional[str] = None,
        country_header: Optional[str] = None,
        local_currencies: bool = False,
        flush_interval_ms: float = 2_000,
        trusted_root_keys: Optional[Mapping[str, str]] = None,
        transport: Union[Transport, Callable[..., Any], None] = None,
        on_error: Optional[Callable[[Exception], Any]] = None,
    ) -> None:
        self._guards = _normalize_guards(products)
        self._disabled = bool(disabled)
        self._api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self._timeout_ms = _milliseconds("timeout_ms", timeout_ms)
        self._refresh_interval_ms = _milliseconds("refresh_interval_ms", refresh_interval_ms)
        self._max_stale_ms = _milliseconds("max_stale_ms", max_stale_ms)
        self._flush_interval_ms = _milliseconds("flush_interval_ms", flush_interval_ms)
        if not isinstance(cookie_name, str) or not _COOKIE_NAME_RE.fullmatch(cookie_name):
            raise ValueError("Leverprice: cookie_name is not a valid cookie name")
        self._cookie_name = cookie_name
        max_age = safe_int(cookie_max_age_seconds)
        if max_age is None or max_age < 0:
            raise ValueError("Leverprice: cookie_max_age_seconds must be a non-negative integer")
        self._cookie_max_age = max_age
        self._cookie_secure = bool(cookie_secure)
        self._cookie_domain = cookie_domain or None
        self._country_header = _header_name(country_header)
        self._local_currencies = local_currencies is True
        self._transport = _transport_callable(transport)
        if on_error is not None and not callable(on_error):
            raise TypeError("Leverprice: on_error must be callable")
        self._on_error = on_error
        roots = ROOT_PUBLIC_KEYS if trusted_root_keys is None else trusted_root_keys
        self._trust = TrustChain(roots, MIN_KEYSET_VERSION)
        key = js_trim(secret_key) if isinstance(secret_key, str) else ""
        self._secret_key: Optional[str] = key if _KEY_RE.fullmatch(key) else None
        self._fingerprint = key_fingerprint(self._secret_key) if self._secret_key else ""

        self._clock: Callable[[], int] = now_ms
        self._request_timeout_ms = REQUEST_TIMEOUT_MS
        self._stall_ms = STALL_MS
        self._send_attempts = SEND_ATTEMPTS
        self._retry_delays = RETRY_DELAYS_S
        self._token = object()

        self._config: Optional[Config] = None
        self._signing_key: Optional[SigningKey] = None
        self._etag: Optional[str] = None
        self._fetched_at = 0
        self._failures = 0
        self._retry_at = 0
        self._server_version = 0
        self._catch_up_at = 0
        self._hmac_cache: Optional[Tuple[str, bytes]] = None
        self._exposed: Set[str] = set()
        self._reset_runtime()
        _clients.add(self)

        if key and self._secret_key is None and not self._disabled and not _env_disabled():
            self._emit(
                LeverpriceError(
                    "Leverprice: secret_key is not a pp_sk_live_… / pp_sk_test_… key; serving fallback prices."
                )
            )

    def _reset_runtime(self) -> None:
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._accept_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._drain_lock = threading.RLock()
        self._scopes = _ScopeMap()
        self._inflight: Optional[_Flight] = None
        self._timer: Optional[threading.Timer] = None
        self._queue: List[Event] = []
        self._flush_pending = False

    def _check_fork(self) -> None:
        if self._pid != os.getpid():
            self._reset_runtime()

    def get_price(
        self,
        product_key: str,
        request: Any = None,
        *,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        user_id: Union[str, int, uuid.UUID, None] = None,
        exposure: str = "auto",
        bot: Optional[bool] = None,
        country: Optional[str] = None,
    ) -> Quote:
        """Returns the price for ``product_key``.

        Never raises because of Leverprice; raises ``UnknownProductError`` for an unconfigured product.
        """
        guard = self._guard(product_key)
        if self._is_off():
            return _fallback(product_key, guard, "disabled")
        self._check_fork()
        ctx, scope = self._context(request, cookie, user_agent, user_id, exposure, bot, country)
        if scope is None:
            return self._run(self._quote_steps(product_key, guard, ctx, None))
        with scope.lock:
            return self._run(self._quote_steps(product_key, guard, ctx, scope))

    async def get_price_async(
        self,
        product_key: str,
        request: Any = None,
        *,
        cookie: Optional[str] = None,
        user_agent: Optional[str] = None,
        user_id: Union[str, int, uuid.UUID, None] = None,
        exposure: str = "auto",
        bot: Optional[bool] = None,
        country: Optional[str] = None,
    ) -> Quote:
        """Async ``get_price``: waiting for a config never blocks the event loop."""
        guard = self._guard(product_key)
        if self._is_off():
            return _fallback(product_key, guard, "disabled")
        self._check_fork()
        ctx, scope = self._context(request, cookie, user_agent, user_id, exposure, bot, country)
        if scope is None:
            return await self._run_async(self._quote_steps(product_key, guard, ctx, None))
        async with scope.async_lock():
            return await self._run_async(self._quote_steps(product_key, guard, ctx, scope))

    def log_exposure(self, quote: Quote) -> bool:
        """Logs a manual-mode exposure once the price is on screen. Returns True if one was queued."""
        try:
            if self._is_off() or quote.reason != "experiment" or quote.exposure_logged:
                return False
            self._check_fork()
            if not quote.experiment_id or not quote.variant_id or not quote.visitor_id:
                return False
            country = normalize_country(getattr(quote, "country", None))
            queued = self._record_exposure(quote.experiment_id, quote.variant_id, quote.visitor_id, country)
            quote.exposure_logged = True
            return queued
        except Exception as err:
            self._report(err)
            return False

    def track_purchase(
        self,
        *,
        provider: str,
        order_id: Union[str, int],
        amount: Any,
        currency: str,
        metadata: Optional[Mapping[str, Any]] = None,
        timestamp: Any = None,
    ) -> None:
        """Reports a completed purchase and sends it right away. Never raises."""
        try:
            if self._is_off():
                return
            self._check_fork()
            self._enqueue(
                {
                    "type": "purchase",
                    "provider": provider,
                    "orderId": _id_of(order_id),
                    "amount": js_round(amount),
                    "currency": str(currency).lower(),
                    "timestamp": self._timestamp_of(timestamp),
                    "attribution": _attribution(metadata),
                }
            )
            self.flush()
        except Exception as err:
            self._report(err)

    def track_refund(
        self,
        *,
        provider: str,
        order_id: Union[str, int],
        currency: str,
        refund_id: Union[str, int, None] = None,
        amount: Any = None,
        refunded_total: Any = None,
        timestamp: Any = None,
    ) -> None:
        """Reports a refund (``refund_id`` + ``amount``, or ``refunded_total``) and sends it. Never raises."""
        try:
            if self._is_off():
                return
            self._check_fork()
            event: Event = {
                "type": "refund",
                "provider": provider,
                "orderId": _id_of(order_id),
                "currency": str(currency).lower(),
                "timestamp": self._timestamp_of(timestamp),
            }
            if refunded_total is not None:
                event["refundedTotal"] = js_round(refunded_total)
            else:
                event["refundId"] = _id_of(refund_id)
                event["amount"] = js_round(amount)
            self._enqueue(event)
            self.flush()
        except Exception as err:
            self._report(err)

    def flush(self) -> None:
        """Sends queued events now and waits until done. Never raises."""
        try:
            self._check_fork()
            self._cancel_timer()
            with self._drain_lock:
                self._drain()
        except Exception as err:
            self._report(err)

    def refresh(self) -> None:
        """Fetches the config now and waits. Never raises."""
        try:
            if self._is_off():
                return
            self._check_fork()
            live = self._live_flight(self._clock())
            if live is not None:
                live.done.wait(self._stall_ms / 1000)
            flight = self._start_fetch(force=True)
            if flight is not None:
                flight.done.wait(self._stall_ms / 1000)
        except Exception as err:
            self._report(err)

    def preload(self) -> None:
        """Fetches the config if none is usable yet; call it at startup. Never raises."""
        try:
            if self._is_off() or self._usable(self._clock()) is not None:
                return
            self._check_fork()
            flight = self._start_fetch(force=True)
            if flight is not None:
                flight.done.wait(self._stall_ms / 1000)
            if self._usable(self._clock()) is None:
                with self._lock:
                    self._retry_at = 0
        except Exception as err:
            self._report(err)

    def close(self) -> None:
        """Sends queued events and stops the flush timer. The client stays usable."""
        self.flush()
        self._cancel_timer()

    async def track_purchase_async(self, **kwargs: Any) -> None:
        """Async ``track_purchase``."""
        if not self._is_off():
            await self._in_thread(functools.partial(self.track_purchase, **kwargs))

    async def track_refund_async(self, **kwargs: Any) -> None:
        """Async ``track_refund``."""
        if not self._is_off():
            await self._in_thread(functools.partial(self.track_refund, **kwargs))

    async def flush_async(self) -> None:
        """Async ``flush``."""
        await self._in_thread(self.flush)

    async def refresh_async(self) -> None:
        """Async ``refresh``."""
        if not self._is_off():
            await self._in_thread(self.refresh)

    async def preload_async(self) -> None:
        """Async ``preload``."""
        if not self._is_off():
            await self._in_thread(self.preload)

    async def close_async(self) -> None:
        """Async ``close``."""
        await self._in_thread(self.close)

    def __enter__(self) -> Leverprice:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def __aenter__(self) -> Leverprice:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close_async()

    async def _in_thread(self, fn: Callable[[], None]) -> None:
        try:
            await asyncio.to_thread(fn)
        except Exception as err:
            self._report(err)

    def _guard(self, product_key: str) -> _Guard:
        guard = self._guards.get(product_key) if isinstance(product_key, str) else None
        if guard is None:
            raise UnknownProductError(f'Leverprice: unknown product "{product_key}". Add it to the products option.')
        return guard

    def _context(
        self, request: Any, cookie: Any, user_agent: Any, user_id: Any, exposure: Any, bot: Any, country: Any
    ) -> Tuple[_Context, Optional[_Scope]]:
        scope: Optional[_Scope] = None
        try:
            request = unwrap(request)
            if is_wsgi_environ(request):
                scopes = request.get("leverprice.scopes")
                if not isinstance(scopes, dict):
                    scopes = request["leverprice.scopes"] = {}
                scope = scopes.get(self._token)
                if scope is None:
                    scope = scopes[self._token] = _Scope()
            elif request is not None and not isinstance(request, (dict, list, tuple, str, bytes)):
                scope = self._scopes.get(request)
        except Exception:
            scope = None
        auto = exposure is None or exposure == "auto"
        return _Context(request, cookie, user_agent, user_id, auto, bot, country), scope

    def _run(self, steps: QuoteSteps) -> Quote:
        config: Optional[Config] = None
        while True:
            try:
                steps.send(config)
            except StopIteration as stop:
                result: Quote = stop.value
                return result
            try:
                config = self._current_config()
            except Exception as err:
                self._report(err)
                config = None

    async def _run_async(self, steps: QuoteSteps) -> Quote:
        config: Optional[Config] = None
        while True:
            try:
                steps.send(config)
            except StopIteration as stop:
                result: Quote = stop.value
                return result
            try:
                config = await self._current_config_async()
            except Exception as err:
                self._report(err)
                config = None

    def _quote_steps(self, product_key: str, guard: _Guard, ctx: _Context, scope: Optional[_Scope]) -> QuoteSteps:
        bot = False
        try:
            source = header_source(ctx.request)
            req = read_request(
                source,
                ctx.cookie,
                ctx.user_agent,
                self._cookie_name,
                ctx.country,
                self._country_header,
            )
            bot = ctx.bot if isinstance(ctx.bot, bool) else is_bot(req.user_agent)
            if bot:
                config = yield
                return self._bot_quote(product_key, guard, config)
            if req.pinned or (scope is not None and scope.pinned):
                self._kick(self._clock())
                return _fallback(product_key, guard, "pinned_fallback", [self._pin_cookie()])
            config = yield
            if config is None:
                return self._no_config(product_key, guard, scope)
            if not isinstance(ctx.country, str) and self._country_header is None and config.country_header is not None:
                req = replace(req, country=normalize_country(read_header(source, config.country_header)))
            product = config.products.get(product_key)
            if product is None:
                return _fallback(product_key, guard, "unknown_product")
            if product.currency != guard.currency:
                return _fallback(product_key, guard, "currency_mismatch")
            exp = _experiment_for(product, req.country)
            if exp is None:
                if not _in_range(product, product.price):
                    return _fallback(product_key, guard, "out_of_bounds")
                region = self._region_for(guard, product, req.country)
                if region is not None:
                    return _regional_quote(product_key, region, req.country)
                return _price_quote(product_key, guard, product.price, "base_price", product.checkout_url, req.country)
            if not exp.variants or not all(_in_range(product, v.price) for v in exp.variants):
                return _fallback(product_key, guard, "out_of_bounds")
            region = self._region_for(guard, product, req.country)
            return self._experiment_quote(product_key, guard, ctx, req, scope, config, product, exp, region)
        except Exception as err:
            self._report(err)
            return _fallback(product_key, guard, "bot") if bot else self._no_config(product_key, guard, scope)

    def _region_for(self, guard: _Guard, product: ConfigProduct, country: Optional[str]) -> Optional[_Region]:
        if country is None:
            return None
        index = product.region_of.get(country)
        if index is None:
            return None
        region = product.regions[index]
        if region.currency != guard.currency and not self._local_currencies:
            return None
        return _Region(index, region.currency, region.price)

    def _experiment_quote(
        self,
        product_key: str,
        guard: _Guard,
        ctx: _Context,
        req: RequestInfo,
        scope: Optional[_Scope],
        config: Config,
        product: ConfigProduct,
        exp: ConfigExperiment,
        region: Optional[_Region],
    ) -> Quote:
        key = self._hmac_key(config.assignment_key)
        user_id = _normalize_user_id(ctx.user_id)
        prior = scope.cookie if scope is not None else None
        state: Optional[AssignmentState] = None
        if (
            prior is not None
            and prior.input == req.cookie_input
            and prior.user_id == user_id
            and prior.assignment_key == config.assignment_key
        ):
            state = prior.state.clone()
        else:
            for raw in req.assignment_cookies[:4]:
                state = verify_assignment(key, raw)
                if state is not None:
                    break
        changed = False
        if state is None:
            state = AssignmentState(secrets.token_hex(16))
            changed = True

        visitor_key = f"u:{user_id}" if user_id is not None else f"a:{state.v}"
        visitor_id = hmac_hex(key, "pp:visitor:v1:" + visitor_key, 32)

        assigned = state.a.get(exp.id)
        sticky = next((v for v in exp.variants if v.id == assigned), None) if assigned is not None else None
        if sticky is None:
            weights = None if req.country is None else exp.split_of.get(req.country)
            variant = _pick_variant(exp.variants, _bucket(exp.salt, visitor_id), weights)
            assign(state, exp.id, variant.id)
            changed = True
        else:
            variant = sticky

        with self._queue_lock:
            logged = exp.id in state.x or f"{exp.id}:{visitor_id}" in self._exposed
        if not logged and ctx.auto_exposure and not req.prefetch:
            self._record_exposure(exp.id, variant.id, visitor_id, req.country)
            logged = True
        if logged and mark_exposed(state, exp.id):
            changed = True

        if scope is not None:
            scope.cookie = _ScopeCookie(req.cookie_input, user_id, config.assignment_key, state)
        signature = hmac_hex(key, f"pp:attr:v1:{exp.id}:{variant.id}:{visitor_id}", 16)
        set_cookie = []
        if changed:
            value = sign_assignment(key, state)
            set_cookie.append(
                set_cookie_header(
                    self._cookie_name, value, self._cookie_max_age, self._cookie_secure, self._cookie_domain
                )
            )
        if region is None:
            price, currency, checkout_url = variant.price, guard.currency, variant.checkout_url or product.checkout_url
        else:
            price, currency, checkout_url = variant.regional[region.index], region.currency, None
        return Quote(
            product_key=product_key,
            price=price,
            currency=currency,
            is_fallback=False,
            reason="experiment",
            experiment_id=exp.id,
            variant_id=variant.id,
            visitor_id=visitor_id,
            metadata={"pp_exp": exp.id, "pp_var": variant.id, "pp_vid": visitor_id, "pp_sig": signature},
            set_cookie=set_cookie,
            checkout_url=checkout_url,
            exposure_logged=logged,
            country=req.country,
            regional=region is not None,
        )

    def _bot_quote(self, product_key: str, guard: _Guard, config: Optional[Config]) -> Quote:
        product = config.products.get(product_key) if config is not None else None
        if product is not None and product.currency == guard.currency and _in_range(product, product.price):
            return _price_quote(product_key, guard, product.price, "bot", product.checkout_url)
        return _fallback(product_key, guard, "bot")

    def _no_config(self, product_key: str, guard: _Guard, scope: Optional[_Scope]) -> Quote:
        if scope is not None:
            scope.pinned = True
        return _fallback(product_key, guard, "no_config", [self._pin_cookie()])

    def _pin_cookie(self) -> str:
        name = self._cookie_name + "_f"
        return set_cookie_header(name, "1", PIN_MAX_AGE_SECONDS, self._cookie_secure, self._cookie_domain)

    def _hmac_key(self, assignment_key: str) -> bytes:
        cached = self._hmac_cache
        if cached is None or cached[0] != assignment_key:
            cached = (assignment_key, from_b64url(assignment_key))
            self._hmac_cache = cached
        return cached[1]

    def _usable_locked(self, now: int) -> Optional[Config]:
        config = self._config
        if config is None or now >= config.expires_at or now - self._fetched_at >= self._max_stale_ms:
            return None
        if self._signing_key is not None and not self._trust.is_trusted(self._signing_key):
            return None
        return config

    def _usable(self, now: int) -> Optional[Config]:
        with self._lock:
            return self._usable_locked(now)

    def _current_config(self) -> Optional[Config]:
        now = self._clock()
        ready = self._usable(now)
        self._kick(now)
        if ready is not None:
            return ready
        flight = self._live_flight(now)
        if flight is None:
            # The fetch started by the kick may already have finished.
            return self._usable(self._clock())
        remaining = self._timeout_ms - (now - flight.started_at)
        if remaining > 0:
            flight.done.wait(remaining / 1000)
        return self._usable(self._clock())

    async def _current_config_async(self) -> Optional[Config]:
        now = self._clock()
        ready = self._usable(now)
        self._kick(now)
        if ready is not None:
            return ready
        flight = self._live_flight(now)
        if flight is None:
            return self._usable(self._clock())
        remaining = self._timeout_ms - (now - flight.started_at)
        if remaining > 0:
            await flight.wait_async(remaining / 1000)
        return self._usable(self._clock())

    def _live_flight_locked(self, now: int) -> Optional[_Flight]:
        flight = self._inflight
        return flight if flight is not None and now - flight.started_at < self._stall_ms else None

    def _live_flight(self, now: int) -> Optional[_Flight]:
        with self._lock:
            return self._live_flight_locked(now)

    def _kick(self, now: int) -> None:
        with self._lock:
            if self._live_flight_locked(now) is not None:
                return
            loaded = self._config.version if self._config is not None else 0
            if self._server_version > loaded and now >= self._catch_up_at:
                self._catch_up_at = now + CATCH_UP_GAP_MS
                self._start_fetch_locked(now, force=False, unconditional=True)
            elif self._usable_locked(now) is None or now - self._fetched_at >= self._refresh_interval_ms:
                self._start_fetch_locked(now, force=False, unconditional=False)

    def _start_fetch(self, force: bool = False) -> Optional[_Flight]:
        now = self._clock()
        with self._lock:
            return self._start_fetch_locked(now, force=force, unconditional=False)

    def _start_fetch_locked(self, now: int, *, force: bool, unconditional: bool) -> Optional[_Flight]:
        live = self._live_flight_locked(now)
        if live is not None:
            return live
        if not force and now < self._retry_at:
            return None
        flight = _Flight(now)
        self._inflight = flight
        thread = threading.Thread(
            target=self._fetch_config, args=(flight, unconditional), name="leverprice-config", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            self._inflight = None
            flight.finish()
            return None
        return flight

    def _fetch_config(self, flight: _Flight, unconditional: bool) -> None:
        try:
            self._load_config(unconditional)
        finally:
            with self._lock:
                if self._inflight is flight:
                    self._inflight = None
            flight.finish()

    def _load_config(self, unconditional: bool) -> None:
        try:
            headers: Dict[str, str] = {}
            with self._lock:
                # Revalidate only a config that is still usable; an expired or revoked one needs a new body.
                if not unconditional and self._etag and self._usable_locked(self._clock()) is not None:
                    headers["If-None-Match"] = self._etag
            res = self._request("/v1/config", "GET", headers)
            if res.status == 304 and "If-None-Match" in headers:
                with self._lock:
                    self._fetched_at = self._clock()
            else:
                if not 200 <= res.status < 300:
                    raise LeverpriceError(f"Leverprice config: HTTP {res.status}{_error_code(res.text)}")
                try:
                    envelope = json_loads(res.text)
                except Exception:
                    raise LeverpriceError("Leverprice config: response is not JSON") from None
                self._accept(envelope, res.headers.get("etag"))
            with self._lock:
                self._failures = 0
                self._retry_at = 0
            self._note_server_version(res.headers)
        except Exception as err:
            with self._lock:
                self._failures += 1
                self._retry_at = self._clock() + min(MAX_BACKOFF_MS, 500 * 2 ** min(self._failures, 16))
            self._report(err)

    def _accept(self, envelope: Any, etag: Optional[str]) -> None:
        notice = None
        with self._accept_lock:
            with self._lock:
                current = self._config
            verified = accept_config(self._trust, self._fingerprint, current, envelope, self._clock())
            config = verified.config
            if verified.dropped and (current is None or config.version != current.version):
                dropped = ", ".join(verified.dropped)
                notice = f"Leverprice config v{config.version}: ignored malformed products {dropped}"
            with self._lock:
                self._config = config
                self._signing_key = verified.signing_key
                self._etag = etag
                self._fetched_at = self._clock()
        if notice:
            self._report(LeverpriceError(notice))

    def _note_server_version(self, headers: Mapping[str, str]) -> None:
        version = parse_header_int(headers.get(CONFIG_VERSION_HEADER))
        if version is None:
            return
        with self._lock:
            if version <= self._server_version:
                return
            self._server_version = version
            newer = version > (self._config.version if self._config is not None else 0)
        if newer:
            self._kick(self._clock())

    def _request(self, path: str, method: str, headers: Dict[str, str], body: Optional[bytes] = None) -> _Response:
        all_headers = {**headers, "Authorization": f"Bearer {self._secret_key}", "User-Agent": USER_AGENT}
        url = self._api_url + path
        timeout = self._request_timeout_ms / 1000
        outcome: Dict[str, Any] = {}
        done = threading.Event()
        transport = self._transport

        def call() -> None:
            try:
                outcome["response"] = transport(method, url, all_headers, body, timeout)
            except Exception as err:
                outcome["error"] = err
            finally:
                done.set()

        try:
            threading.Thread(target=call, name="leverprice-http", daemon=True).start()
        except RuntimeError:
            call()
        if not done.wait(timeout):
            raise LeverpriceError(f"Leverprice: {method} {path} timed out after {self._request_timeout_ms} ms")
        error = outcome.get("error")
        if isinstance(error, LeverpriceError):
            raise error
        if error is not None:
            raise LeverpriceError(f"Leverprice: {method} {path} failed: {error}") from error
        if "response" not in outcome:
            raise LeverpriceError(f"Leverprice: {method} {path} failed")
        return _normalize_response(outcome["response"])

    def _timestamp_of(self, t: Any) -> Optional[int]:
        if t is None:
            return self._clock()
        if isinstance(t, datetime):
            return js_round(t.timestamp() * 1000)
        if isinstance(t, (bool, str, bytes)):
            return None
        try:
            return js_round(t * 1000 if t < 1e12 else t)
        except TypeError:
            return None

    def _record_exposure(
        self, experiment_id: str, variant_id: str, visitor_id: str, country: Optional[str] = None
    ) -> bool:
        key = f"{experiment_id}:{visitor_id}"
        with self._queue_lock:
            if key in self._exposed:
                return False
            if len(self._exposed) >= MAX_DEDUPE:
                self._exposed.clear()
            self._exposed.add(key)
        event: Event = {
            "type": "exposure",
            "experimentId": experiment_id,
            "variantId": variant_id,
            "visitorId": visitor_id,
            "timestamp": self._clock(),
        }
        if country:
            event["country"] = country
        return self._enqueue(event)

    def _enqueue(self, event: Event) -> bool:
        problem = event_problem(event)
        if problem:
            self._report(LeverpriceError(f"Leverprice: dropped invalid {event.get('type')} event: {problem}"))
            return False
        with self._queue_lock:
            self._queue.append(event)
            dropped = self._trim_locked()
            flush_now = len(self._queue) >= AUTO_FLUSH_AT
            if not flush_now and self._timer is None:
                self._arm_timer_locked()
        self._report_dropped(dropped)
        if flush_now:
            self._flush_soon()
        return True

    def _trim_locked(self) -> List[Event]:
        dropped = []
        while len(self._queue) > MAX_QUEUE:
            index = next((i for i, e in enumerate(self._queue) if e.get("type") == "exposure"), 0)
            event = self._queue.pop(index)
            if event.get("type") != "exposure":
                dropped.append(event)
        return dropped

    def _report_dropped(self, dropped: List[Event]) -> None:
        for event in dropped:
            message = f"Leverprice: event queue full, dropped {event.get('type')} {event.get('orderId')}"
            self._report(LeverpriceError(message))

    def _arm_timer_locked(self) -> None:
        timer = threading.Timer(self._flush_interval_ms / 1000, self._on_timer)
        timer.daemon = True
        timer.name = "leverprice-flush-timer"
        self._timer = timer
        try:
            timer.start()
        except RuntimeError:
            self._timer = None

    def _on_timer(self) -> None:
        with self._queue_lock:
            if self._timer is threading.current_thread():
                self._timer = None
        self.flush()

    def _cancel_timer(self) -> None:
        with self._queue_lock:
            timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _flush_soon(self) -> None:
        with self._queue_lock:
            if self._flush_pending:
                return
            self._flush_pending = True
        try:
            threading.Thread(target=self._background_flush, name="leverprice-flush", daemon=True).start()
        except RuntimeError:
            with self._queue_lock:
                self._flush_pending = False

    def _background_flush(self) -> None:
        try:
            self._cancel_timer()
            with self._drain_lock:
                with self._queue_lock:
                    self._flush_pending = False
                self._drain()
        except Exception as err:
            self._report(err)

    def _drain(self) -> None:
        if self._is_off():
            return
        with self._queue_lock:
            batches = -(-len(self._queue) // MAX_EVENTS_PER_REQUEST)
        for _ in range(batches):
            with self._queue_lock:
                if not self._queue:
                    return
                batch = self._queue[:MAX_EVENTS_PER_REQUEST]
                del self._queue[:MAX_EVENTS_PER_REQUEST]
            if not self._send(batch):
                return

    def _requeue(self, events: List[Event]) -> None:
        money = [e for e in events if e.get("type") != "exposure"]
        if not money:
            return
        with self._queue_lock:
            self._queue[:0] = money
            dropped = self._trim_locked()
        self._report_dropped(dropped)

    def _send(self, events: List[Event]) -> bool:
        last_error: Optional[Exception] = None
        try:
            body = _events_body(events)
            for attempt in range(self._send_attempts):
                if self._is_off():
                    break
                if attempt > 0:
                    threading.Event().wait(self._retry_delays[min(attempt, len(self._retry_delays)) - 1])
                try:
                    res = self._request("/v1/events", "POST", {"Content-Type": "application/json"}, body)
                except Exception as err:
                    last_error = err
                    continue
                self._note_server_version(res.headers)
                if 200 <= res.status < 300:
                    return True
                if res.status < 500 and res.status not in (408, 429):
                    self._report(LeverpriceError(f"Leverprice events rejected: HTTP {res.status} {res.text[:300]}"))
                    return True
                last_error = LeverpriceError(f"Leverprice events: HTTP {res.status}")
        except Exception as err:
            last_error = err
        self._requeue(events)
        if last_error is not None:
            self._report(last_error)
        return False

    def _flush_at_exit(self) -> None:
        if self._pid != os.getpid() or self._is_off():
            return
        with self._queue_lock:
            if not self._queue:
                return
        self._send_attempts = 1
        self._request_timeout_ms = min(self._request_timeout_ms, EXIT_FLUSH_TIMEOUT_MS)
        if self._drain_lock.acquire(timeout=EXIT_FLUSH_TIMEOUT_MS / 1000):
            try:
                self._drain()
            finally:
                self._drain_lock.release()

    def _is_off(self) -> bool:
        return self._disabled or self._secret_key is None or _env_disabled()

    def _report(self, err: Exception) -> None:
        if not self._is_off():
            self._emit(err)

    def _emit(self, err: Exception) -> None:
        try:
            if self._on_error is not None:
                self._on_error(err)
            else:
                _logger.warning("%s", err)
        except Exception:
            pass


def _events_body(events: List[Event]) -> bytes:
    payload = {"events": events, "sdk": {"name": SDK_NAME, "version": SDK_VERSION}}
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _env_disabled() -> bool:
    value = os.environ.get("LEVERPRICE_DISABLED")
    return value is not None and js_trim(value).lower() in ("1", "true")


def _milliseconds(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        raise ValueError(f"Leverprice: {name} must be a non-negative number of milliseconds")
    return value


def _strict_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else None


def _normalize_guards(products: Any) -> Dict[str, _Guard]:
    if products is None:
        products = {}
    if not isinstance(products, Mapping):
        raise TypeError("Leverprice: products must be a mapping of product key to guard")
    guards: Dict[str, _Guard] = {}
    for key, guard in products.items():
        if not isinstance(key, str):
            raise TypeError(f"Leverprice: product keys must be strings, got {key!r}")
        where = f"Leverprice: products.{key}"
        if isinstance(guard, ProductGuard):
            values = {f: getattr(guard, f) for f in _GUARD_FIELDS}
        elif isinstance(guard, Mapping):
            unknown = sorted(str(k) for k in guard if k not in _GUARD_FIELDS and k not in _DEPRECATED_GUARD_FIELDS)
            if unknown:
                raise ValueError(f"{where}: unknown option {', '.join(unknown)}")
            values = {f: guard.get(f) for f in _GUARD_FIELDS}
        else:
            raise TypeError(f"{where} must be a ProductGuard or a dict")
        currency = values["currency"]
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            raise ValueError(f'{where}.currency must be an ISO 4217 code such as "usd"')
        fallback = _strict_int(values["fallback_price"])
        if fallback is None or fallback <= 0:
            raise ValueError(f"{where}.fallback_price must be a positive integer (minor units)")
        guards[key] = _Guard(currency.lower(), fallback)
    return guards


def _header_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    name = value.strip(ASCII_WHITESPACE) if isinstance(value, str) else None
    if name is None or (name and not _HEADER_NAME_RE.fullmatch(name)):
        raise ValueError("Leverprice: country_header is not a valid header name")
    # Lowercased only after the check, so case mapping never sees anything but ASCII.
    return name.lower() or None


def _transport_callable(transport: Any) -> Callable[..., Any]:
    if transport is None:
        transport = UrllibTransport()
    method: Any = getattr(transport, "request", None)
    if callable(method):
        return method  # type: ignore[no-any-return]
    if callable(transport):
        return transport  # type: ignore[no-any-return]
    raise TypeError("Leverprice: transport must provide request(method, url, headers, body, timeout)")


def _normalize_response(res: Any) -> _Response:
    status = getattr(res, "status", None)
    if status is None:
        status = getattr(res, "status_code", None)
    raw_headers = getattr(res, "headers", None) or {}
    headers: Dict[str, str] = {}
    items = getattr(raw_headers, "items", None)
    if callable(items):
        for name, value in items():
            headers[str(name).lower()] = value if isinstance(value, str) else str(value)
    body = getattr(res, "body", None)
    if body is None:
        body = getattr(res, "content", None)
    if body is None:
        body = getattr(res, "text", b"")
    text = bytes(body).decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    return _Response(int(status), headers, text)  # type: ignore[arg-type]


def _error_code(body: str) -> str:
    try:
        data = json_loads(body)
    except Exception:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return f" ({code})" if isinstance(code, str) else ""


def _metadata_value(metadata: Any, key: str) -> Any:
    getter = getattr(metadata, "get", None)
    if not callable(getter) or isinstance(metadata, (str, bytes)):
        return None
    try:
        return getter(key)
    except Exception:
        return None


def _metadata_mapping(metadata: Any) -> Any:
    """Stripe objects are not dicts since stripe-python 15 and have ``to_dict()`` instead of ``get()``."""
    try:
        if callable(getattr(metadata, "get", None)):
            return metadata
        to_dict = getattr(metadata, "to_dict", None)
        return to_dict() if callable(to_dict) else None
    except Exception:
        return None


def _attribution(metadata: Any) -> Optional[Dict[str, Any]]:
    metadata = _metadata_mapping(metadata)
    ids = [_metadata_value(metadata, k) for k in ("pp_exp", "pp_var", "pp_vid")]
    if not all(is_id(i) for i in ids):
        return None
    sig = _metadata_value(metadata, "pp_sig")
    return {
        "experimentId": ids[0],
        "variantId": ids[1],
        "visitorId": ids[2],
        "signature": sig if isinstance(sig, str) and _SIG_RE.fullmatch(sig) else None,
    }


def _experiment_for(product: ConfigProduct, country: Optional[str]) -> Optional[ConfigExperiment]:
    exp = product.experiment
    if exp is None or exp.scope is None:
        return exp
    return exp if country is not None and country in exp.scope else None


def _in_range(product: ConfigProduct, price: int) -> bool:
    return safe_int(price) is not None and price > 0 and product.min_price <= price <= product.max_price


def _fallback(product_key: str, guard: _Guard, reason: str, set_cookie: Optional[List[str]] = None) -> Quote:
    return Quote(
        product_key=product_key,
        price=guard.fallback_price,
        currency=guard.currency,
        is_fallback=True,
        reason=reason,
        set_cookie=list(set_cookie or []),
    )


def _price_quote(
    product_key: str,
    guard: _Guard,
    price: int,
    reason: str,
    checkout_url: Optional[str],
    country: Optional[str] = None,
) -> Quote:
    return Quote(
        product_key=product_key,
        price=price,
        currency=guard.currency,
        is_fallback=False,
        reason=reason,
        checkout_url=checkout_url,
        country=country,
    )


def _regional_quote(product_key: str, region: _Region, country: Optional[str]) -> Quote:
    return Quote(
        product_key=product_key,
        price=region.price,
        currency=region.currency,
        is_fallback=False,
        reason="base_price",
        country=country,
        regional=True,
    )


def _normalize_user_id(user_id: Any) -> Optional[str]:
    if isinstance(user_id, bool):
        return None
    if isinstance(user_id, int):
        return str(user_id)
    if isinstance(user_id, float) and user_id.is_integer() and abs(user_id) <= MAX_SAFE_INTEGER:
        return str(int(user_id))
    if isinstance(user_id, uuid.UUID):
        return str(user_id)
    return user_id if isinstance(user_id, str) and user_id else None


def _id_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _bucket(salt: str, visitor_id: str) -> int:
    return int.from_bytes(sha256(f"{salt}:{visitor_id}")[:4], "big") % 10_000


def _pick_variant(
    variants: Tuple[ConfigVariant, ...], bucket: int, weights: Optional[Tuple[int, ...]] = None
) -> ConfigVariant:
    if weights is None:
        weights = tuple(variant.weight for variant in variants)
    total = 0
    for variant, weight in zip(variants, weights):
        total += weight
        if bucket < total:
            return variant
    return variants[-1]


def _reset_after_fork() -> None:
    for client in list(_clients):
        client._reset_runtime()


def _flush_all_at_exit() -> None:
    for client in list(_clients):
        with contextlib.suppress(Exception):
            client._flush_at_exit()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
atexit.register(_flush_all_at_exit)
