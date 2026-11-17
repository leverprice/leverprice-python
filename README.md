<p>
<a href="https://leverprice.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/leverprice/.github/main/assets/repo/python-dark.svg">
    <img alt="Leverprice SDK for Python" src="https://raw.githubusercontent.com/leverprice/.github/main/assets/repo/python-light.svg" width="100%">
  </picture>
</a>
</p>

<p>
  <a href="https://pypi.org/project/leverprice/"><img alt="PyPI" src="https://img.shields.io/pypi/v/leverprice?label=pypi&style=flat-square&labelColor=0F1011&color=5B4CF5"></a>
  <a href="https://github.com/leverprice/leverprice-python/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/leverprice/leverprice-python/ci.yml?branch=main&label=ci&style=flat-square&labelColor=0F1011"></a>
  <a href="https://github.com/leverprice/leverprice-python/blob/main/LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-5B4CF5?style=flat-square&labelColor=0F1011"></a>
</p>

Server-side SDK for [Leverprice](https://leverprice.com) price experiments. No required dependencies, Python 3.9+, works with Django, Flask, FastAPI, Starlette, aiohttp and plain WSGI/ASGI.

**Your payments stay yours.** The SDK gives your server a number. Your own code creates the checkout with your own provider keys. Leverprice never sees card data or provider API keys.

## Installation

```sh
pip install leverprice
```

Signature checks use [`cryptography`](https://pypi.org/project/cryptography/) when it is installed and a bundled pure-Python verifier otherwise. To install it alongside: `pip install "leverprice[crypto]"`.

## Quick start

Create one client per process (for example in `settings.py` or `app.py`) and share it:

```python
import os
from leverprice import Leverprice

leverprice = Leverprice(
    secret_key=os.environ.get("LEVERPRICE_SECRET_KEY"),  # server-only
    products={
        # Local guardrails: prices outside these bounds are never returned.
        "pro": {"currency": "usd", "fallback_price": 1900, "min_price": 1500, "max_price": 3900},
    },
)
leverprice.preload()  # optional: fetch the config at startup instead of on the first request
```

### 1. Show the price

`get_price` reads the visitor's cookie and user agent from the request. Append `quote.set_cookie` to your response; `apply_cookies` does it for the common frameworks.

**Django**

```python
from django.shortcuts import render
from leverprice import apply_cookies


def pricing(request):
    user_id = request.user.pk if request.user.is_authenticated else None
    quote = leverprice.get_price("pro", request, user_id=user_id)
    return apply_cookies(render(request, "pricing.html", {"quote": quote}), quote)
```

**Flask**

```python
from flask import make_response, render_template, request


@app.get("/pricing")
def pricing():
    quote = leverprice.get_price("pro", request)
    response = make_response(render_template("pricing.html", quote=quote))
    for cookie in quote.set_cookie:
        response.headers.add("Set-Cookie", cookie)
    return response
```

**FastAPI**

```python
from fastapi import FastAPI, Request, Response


@app.get("/pricing")
async def pricing(request: Request, response: Response):
    quote = await leverprice.get_price_async("pro", request)
    for cookie in quote.set_cookie:
        response.headers.append("set-cookie", cookie)
    return {"price": quote.price, "currency": quote.currency}
```

### 2. Create the checkout

Price the checkout on the server again. Never trust a price sent by the browser.

```python
import stripe

quote = leverprice.get_price("pro", request)
session = stripe.checkout.Session.create(
    mode="payment",
    line_items=[
        {
            "quantity": 1,
            "price_data": {"currency": quote.currency, "unit_amount": quote.price, "product_data": {"name": "Pro"}},
        }
    ],
    metadata=quote.metadata,  # signed attribution: pass it whole
    payment_intent_data={"metadata": quote.metadata},
    success_url=success_url,
    cancel_url=cancel_url,
)
```

### 3. Report the purchase

In your webhook handler (or connect your provider's webhooks to Leverprice directly):

```python
event = stripe.Webhook.construct_event(payload, signature_header, endpoint_secret)
if event["type"] == "checkout.session.completed":
    session = event["data"]["object"]
    leverprice.track_purchase(
        provider="stripe",
        order_id=session["payment_intent"],
        amount=session["amount_total"] - (session["total_details"] or {}).get("amount_tax", 0),
        currency=session["currency"],
        metadata=session["metadata"],
        timestamp=event["created"],  # Unix seconds are detected
    )
```

Refunds: `track_refund(provider=..., order_id=..., currency=..., refund_id=..., amount=...)`, or `refunded_total=...` for the order's cumulative refunded amount.

## Guarantees

- **Fails safe.** If Leverprice is off, slow (no config within 500 ms), down, misconfigured or returns anything unexpected, `get_price` returns your `fallback_price`. It never raises for those reasons.
- **Verified config.** Root key → keyset → config signature chain, version-monotonic (no rollback, no revoked keys), time-limited.
- **Bounded.** Every price is checked against your local `min_price`, `max_price` and `currency`.
- **Sticky and tamper-proof.** Visitors keep their price. The assignment cookie is HMAC-signed with your project's assignment key, so rotating your API key does not reshuffle visitors.
- **Private.** Visitor and user ids are HMAC-hashed on your server; Leverprice only sees the hash.
- **No price flips.** A visitor who got the fallback during an outage keeps it for the rest of their session.
- **Search engines see the standard price.** Crawlers and link previews are never put into experiments.
- **Never blocks your process.** Background work runs on daemon threads; the async API never blocks the event loop.

## Requests and cookies

Pass the framework's request object: Django `HttpRequest`, Flask/Werkzeug `request`, Starlette/FastAPI `Request`, aiohttp `Request`, a WSGI `environ`, an ASGI scope, any object with a `headers` mapping, or a plain dict of headers. Header names are case-insensitive. `cookie=` and `user_agent=` override what is read from the request.

`quote.set_cookie` is a **list** of `Set-Cookie` header values: usually empty, one value when the assignment cookie (`__pp`) changes, or the outage pin (`__pp_f`). Append every value. `apply_cookies(response, quote)` supports Django, Flask/Werkzeug, Starlette/FastAPI and aiohttp responses and WSGI header lists. Django has no API for raw `Set-Cookie` values, so there it loads them into `response.cookies`.

Pricing several products for one page? Pass the **same request object** to each `get_price` call: the quotes share one visitor, and the last quote's cookie carries every assignment. Plain header dicts are not shared; pass the request itself.

## Exposures

An exposure is one person seeing an experiment price. With `exposure="auto"` (default) the SDK logs it the first time a visitor gets a price from an experiment, once per visitor and experiment. Prefetch and prerender requests (`Sec-Purpose`, `Purpose`, `X-Purpose`, `X-Moz`, `Next-Router-Prefetch`) are priced but not counted.

If you compute the price before it is visible, use manual mode:

```python
quote = leverprice.get_price("pro", request, exposure="manual")
# later, when the price is on screen:
leverprice.log_exposure(quote)
```

Exposures are sent in the background within `flush_interval_ms` (2 s). Call `flush()` before a short-lived process exits; long-lived servers need nothing.

## Bots

Crawlers, link previewers, headless browsers, uptime monitors and command-line clients get the product's standard price (or your fallback if no config is available) with reason `"bot"`: no assignment, no cookie, no exposure. An empty user agent counts as a person. Pass `bot=True` or `bot=False` to use your own detection; the built-in check is `leverprice.is_bot(user_agent)`.

## Kill switch

Set `LEVERPRICE_DISABLED=1` (or `true`) in the environment, or pass `disabled=True`. It is read on every call: `get_price` returns your fallback price with reason `"disabled"`, sets no cookies, makes no network calls and logs nothing; tracking calls do nothing. A missing or malformed `secret_key` behaves the same way (a malformed key is reported once through `on_error`).

## Outages

When no usable config is available (reason `"no_config"`), the quote also sets `__pp_f=1` for 30 minutes. While a browser sends it, it gets the fallback price with reason `"pinned_fallback"` and is not enrolled; each pinned request extends it, so the price changes only after 30 minutes of inactivity. Purchases and refunds that cannot be delivered are kept and retried on the next flush.

## Trust chain and updates

The SDK embeds only Leverprice's offline root public keys (`ROOT_PUBLIC_KEYS`). Each config carries a keyset of online signing keys signed by a root key, and must be signed by one of those keys. Keyset versions only move forward within a process, so a revoked key cannot come back; `MIN_KEYSET_VERSION` sets the floor for fresh processes. Config versions only move forward, and configs expire. For local development pass `trusted_root_keys={"r1": "<dev root public key>"}`.

The config is revalidated every 30 s with an ETag. When any Leverprice response announces a newer version in `Leverprice-Config-Version`, the SDK fetches it right away.

## Threads, asyncio and forking servers

The client is thread-safe. Every method has an `_async` variant for asyncio apps (`get_price_async`, `track_purchase_async`, `track_refund_async`, `flush_async`, `refresh_async`, `preload_async`, `close_async`). The client works under pre-forking servers (gunicorn, uWSGI): a child process starts with fresh threads, locks and an empty event queue. It also works as a context manager (`with` / `async with`), which flushes on exit.

## Reference

`get_price(product_key, request=None, *, cookie=None, user_agent=None, user_id=None, exposure="auto", bot=None) -> Quote`. Raises `UnknownProductError` only if `product_key` is missing from `products`.

| Quote field | |
|---|---|
| `product_key` | The key you asked for |
| `price` | Minor units; use it for display and as the checkout amount |
| `currency` | Lowercase ISO 4217, from your product guard |
| `is_fallback` | `True` when `price` is your `fallback_price` |
| `reason` | See below |
| `experiment_id`, `variant_id` | Set when `reason` is `"experiment"`, else `None` |
| `visitor_id` | Hashed visitor id Leverprice sees; `None` unless `reason` is `"experiment"` |
| `metadata` | `{pp_exp, pp_var, pp_vid, pp_sig}` for the checkout (signed); `{}` otherwise |
| `set_cookie` | `Set-Cookie` values to append (zero or more) |
| `checkout_url` | Hosted checkout link for this price, or `None` |
| `exposure_logged` | An exposure for this visitor and experiment has been logged |

| `reason` | Price |
|---|---|
| `experiment` | The visitor's sticky variant |
| `base_price` | Standard price (no experiment running) |
| `bot` | Standard price, or fallback if unavailable; never enrolled |
| `disabled` | Fallback: kill switch, or no valid secret key |
| `no_config` | Fallback: no usable signed config (timeout, outage, rejected or expired); sets the pin |
| `pinned_fallback` | Fallback: this browser got a fallback during an outage in this session |
| `unknown_product` | Fallback: the config has no such product |
| `currency_mismatch` | Fallback: the config's currency differs from your guard |
| `out_of_bounds` | Fallback: a price is outside your (or Leverprice's) min/max |

| Method | |
|---|---|
| `log_exposure(quote)` | Logs a manual-mode exposure; returns `True` if one was queued |
| `track_purchase(*, provider, order_id, amount, currency, metadata=None, timestamp=None)` | Reads `pp_exp`, `pp_var`, `pp_vid`, `pp_sig` from `metadata`; sends right away |
| `track_refund(*, provider, order_id, currency, refund_id=None, amount=None, refunded_total=None, timestamp=None)` | Sends right away |
| `flush()` | Sends queued events and waits (at most 500 per request; purchases and refunds are kept on failure) |
| `refresh()` / `preload()` | Fetch the config now / only if none is usable |
| `close()` | Flushes and stops the flush timer |

Providers: `stripe`, `polar`, `lemonsqueezy`, `dodo`, `other`. Timestamps are milliseconds, Unix seconds or a `datetime`. None of these methods raise.

| Option | Default | |
|---|---|---|
| `secret_key` | required | `pp_sk_live_…` or `pp_sk_test_…` |
| `products` | required | Product key → `{currency, fallback_price, min_price?, max_price?}` or `ProductGuard` |
| `disabled` | `False` | Kill switch |
| `api_url` | `https://api.leverprice.com` | |
| `timeout_ms` | `500` | Longest wait for a config when none is usable |
| `refresh_interval_ms` | `30000` | Config revalidation period |
| `max_stale_ms` | `86400000` | Stop using a config this long after the server last confirmed it |
| `cookie_name` | `"__pp"` | The outage pin is `<cookie_name>_f` |
| `cookie_max_age_seconds` | `15552000` | 180 days |
| `cookie_secure` | `True` | Set `False` only for plain-HTTP development |
| `cookie_domain` | `None` | |
| `flush_interval_ms` | `2000` | Longest time exposures wait before being sent |
| `trusted_root_keys` | `ROOT_PUBLIC_KEYS` | For staging and local development |
| `transport` | `UrllibTransport()` | Any object with `request(method, url, headers, body, timeout)` |
| `on_error` | log a warning | Receives every SDK error; exceptions it raises are ignored |

Errors are logged on the `leverprice` logger when no `on_error` is given.

## License

MIT
