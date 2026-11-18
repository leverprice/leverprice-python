<p>
<a href="https://leverprice.com">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/leverprice/.github/main/assets/repo/python-dark.svg">
    <img alt="Leverprice SDK for Python" src="https://raw.githubusercontent.com/leverprice/.github/main/assets/repo/python-light.svg" width="100%">
  </picture>
</a>
</p>

<p>
  <a href="https://pypi.org/project/leverprice/"><img alt="PyPI" src="https://img.shields.io/pypi/v/leverprice?label=pypi&style=flat-square&labelColor=0F1011&color=3C3F44"></a>
  <a href="https://github.com/leverprice/leverprice-python/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/leverprice/leverprice-python/ci.yml?branch=main&label=ci&style=flat-square&labelColor=0F1011"></a>
  <a href="https://github.com/leverprice/leverprice-python/blob/main/LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-3C3F44?style=flat-square&labelColor=0F1011"></a>
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
        "pro": {"currency": "usd", "fallback_price": 1900},  # your current price, served whenever Leverprice cannot answer
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
import os

from stripe import StripeClient

stripe_client = StripeClient(os.environ["STRIPE_SECRET_KEY"])

quote = leverprice.get_price("pro", request)
session = stripe_client.v1.checkout.sessions.create(
    {
        "mode": "payment",
        "line_items": [
            {
                "quantity": 1,
                "price_data": {"currency": quote.currency, "unit_amount": quote.price, "product_data": {"name": "Pro"}},
            }
        ],
        "metadata": quote.metadata,  # signed attribution: pass it whole
        "payment_intent_data": {"metadata": quote.metadata},
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
)
```

### 3. Report the purchase

In your webhook handler (or connect your provider's webhooks to Leverprice directly). `payload` is the raw request body, exactly as received:

```python
PAID = ("checkout.session.completed", "checkout.session.async_payment_succeeded")

event = stripe_client.construct_event(payload, signature_header, endpoint_secret)
if event.type in PAID:
    session = event.data.object
    if session.payment_status == "paid" and session.payment_intent and session.amount_total > 0:
        tax = session.total_details.amount_tax if session.total_details else 0
        leverprice.track_purchase(
            provider="stripe",
            order_id=session.payment_intent,
            amount=session.amount_total - tax,
            currency=session.currency,
            metadata=session.metadata.to_dict() if session.metadata else None,  # not a dict since stripe-python 15
            timestamp=event.created,  # Unix seconds are detected
        )
```

A Checkout Session paid with a delayed method (bank debit, voucher) completes as `unpaid`. Its payment is reported by `checkout.session.async_payment_succeeded`, which is why both events are handled and only `paid` sessions count. $0 sessions (a trial start, a 100% discount) are not sales.

A subscription session names the PaymentIntent of its first payment from Stripe API version 2025-03-31.basil on. On an older version, report that payment from `invoice.paid` with `billing_reason` `subscription_create`: the order id is the invoice's `payment_intent` and the attribution is in `subscription_details.metadata`, so pass `"subscription_data": {"metadata": quote.metadata}` at checkout as well.

Refunds: `track_refund(provider=..., order_id=..., currency=..., refund_id=..., amount=...)`, or `refunded_total=...` for the order's cumulative refunded amount.

That is all the configuration your code holds. Prices, price ranges, tests, regional prices and where the visitor's country comes from are managed in Leverprice and arrive in a signed config, so new features and new settings never need a code change.

## Guarantees

- **Fails safe.** If Leverprice is off, slow (no config within 500 ms), down, misconfigured or returns anything unexpected, `get_price` returns your `fallback_price`. It never raises for those reasons.
- **Verified config.** Root key → keyset → config signature chain, version-monotonic (no rollback, no revoked keys), time-limited.
- **Bounded.** Every standard and test price is checked against the product's signed price range and currency; regional prices follow from them, and every country pays between 5% and 200% of the standard price.
- **Sticky and tamper-proof.** Visitors keep their price. The assignment cookie is HMAC-signed with your project's assignment key, so rotating your API key does not reshuffle visitors.
- **Private.** Visitor and user ids are HMAC-hashed on your server; Leverprice only sees the hash.
- **No price flips.** A visitor who got the fallback during an outage keeps it for the rest of their session.
- **Search engines see the standard price.** Crawlers and link previews are never put into experiments.
- **Never blocks your process.** Background work runs on daemon threads; the async API never blocks the event loop.

## Requests and cookies

Pass the framework's request object: Django `HttpRequest`, Flask/Werkzeug `request`, Starlette/FastAPI `Request`, aiohttp `Request`, a WSGI `environ`, an ASGI scope, any object with a `headers` mapping, or a plain dict of headers. Header names are case-insensitive. `cookie=` and `user_agent=` override what is read from the request.

`quote.set_cookie` is a **list** of `Set-Cookie` header values: usually empty, one value when the assignment cookie (`__pp`) changes, or the outage pin (`__pp_f`). Append every value. `apply_cookies(response, quote)` supports Django, Flask/Werkzeug, Starlette/FastAPI and aiohttp responses and WSGI header lists. Django has no API for raw `Set-Cookie` values, so there it loads them into `response.cookies`.

Pricing several products for one page? Pass the **same request object** to each `get_price` call: the quotes share one visitor, and the last quote's cookie carries every assignment. Plain header dicts are not shared; pass the request itself.

A visitor who refuses cookies still keeps one price and counts once: without a cookie, the SDK keys the visitor by an HMAC of their IP address (an IPv6 address by its /64 network), browser and languages, made with your project's secret on your server. Neither the address nor the browser leaves your server. The address comes from `CF-Connecting-IP`, `True-Client-IP`, `Fastly-Client-IP`, `Fly-Client-IP`, `X-Real-IP`, `X-Forwarded-For` or the connection (`REMOTE_ADDR`, `request.remote_addr`, `request.client.host`).

## Regional prices

Turn on regional prices for a product in the dashboard and a visitor from India pays $12, or ₹999 in rupees, where a visitor from the US pays $29. Your code stays as it is: in Leverprice, choose where your site runs under **Project settings → Visitor country** (Cloudflare, Vercel, AWS CloudFront, or the header your own proxy sets), and the SDK reads the visitor's country from that header.

```python
quote = leverprice.get_price("pro", request)  # a visitor from India
quote.price  # 1200: India is set to $12 in the dashboard
quote.currency  # "usd": pass both to your checkout
quote.country  # "IN"
quote.regional  # True
```

- **The country.** Name only a header your host or CDN sets and overwrites: a header a visitor can send themselves would let them pick their own price. Headers every browser sends (`Accept-Language`, `X-Forwarded-For`, `User-Agent`…) are never read as the country: as `country_header` they are ignored and reported to `on_error`. To decide in code instead, pass `country_header` to the constructor (it replaces the project setting) or `country=` to `get_price` (it wins over both). Under WSGI, `CF_IPCountry` and `CF-IPCountry` arrive as the same key, so keep header names with underscores dropped in front of the app (the nginx default).
- **VPNs.** A visitor keeps the price of a dearer country they were seen in during the last 7 days, so turning on a VPN to a cheaper country doesn't lower it. That country is `quote.country`, and it travels in the signed `__pp` cookie, which a regional product now sets even without a test.
- **Local currencies.** Regional prices in your product's currency ($12 in India instead of $29) need nothing in code. Prices in the visitor's own currency (₹999) are served only with `local_currencies=True`, which you set when your checkout charges `quote.price` in `quote.currency`; without it those visitors get the standard price, so a checkout with a fixed currency can never charge 99900 in dollars.
- **Tests.** Each visitor still gets one sticky variant, at that variant's price for their country. A test can run in selected countries only, and countries can get their own share of traffic per price: both are set in the dashboard and need nothing from your code but the visitor's country.
- **Checkout.** Always pass `quote.currency` with `quote.price`, also before you turn regional prices on: then they need no code change later. `checkout_url` is `None` for a regional price: hosted checkout links are made for standard prices.
- Bots and visitors whose country is unknown get the standard price.

Behind Cloudflare the header is `cf-ipcountry`, on Vercel `x-vercel-ip-country`, behind CloudFront `cloudfront-viewer-country` (once it is added to the origin request policy). `country_header` takes the HTTP header name; under WSGI and Django the SDK finds `HTTP_CF_IPCOUNTRY` for you.

## Exposures

An exposure is one person seeing an experiment price. With `exposure="auto"` (default) the SDK logs it the first time a visitor gets a price from an experiment, once per visitor and experiment. Prefetch and prerender requests (`Sec-Purpose`, `Purpose`, `X-Purpose`, `X-Moz`, `Next-Router-Prefetch`, and Turbo's `X-Sec-Purpose`) are priced but not counted.

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

`get_price(product_key, request=None, *, cookie=None, user_agent=None, user_id=None, exposure="auto", bot=None, country=None) -> Quote`. Raises `UnknownProductError` only if `product_key` is missing from `products`.

| Quote field | |
|---|---|
| `product_key` | The key you asked for |
| `price` | Minor units; use it for display and as the checkout amount |
| `currency` | Lowercase ISO 4217: your product's currency, or the region's for a regional price |
| `country` | The country whose price applies (ISO 3166-1 alpha-2): the visitor's, or a dearer one they hold (see VPNs); `None` when unknown |
| `regional` | `True` when `price` is the regional price of `country` |
| `is_fallback` | `True` when `price` is your `fallback_price` |
| `reason` | See below |
| `experiment_id`, `variant_id` | Set when `reason` is `"experiment"`, else `None` |
| `visitor_id` | Hashed visitor id Leverprice sees; `None` unless `reason` is `"experiment"` |
| `metadata` | `{pp_exp, pp_var, pp_vid, pp_sig}` for the checkout (signed); `{}` otherwise |
| `set_cookie` | `Set-Cookie` values to append (zero or more) |
| `checkout_url` | Hosted checkout link for this price, or `None` (always `None` for a regional price) |
| `exposure_logged` | An exposure for this visitor and experiment has been logged |

| `reason` | Price |
|---|---|
| `experiment` | The visitor's sticky variant |
| `base_price` | The product's price for this visitor (no experiment runs for them) |
| `bot` | Standard price, or fallback if unavailable; never enrolled |
| `disabled` | Fallback: kill switch, or no valid secret key |
| `no_config` | Fallback: no usable signed config (timeout, outage, rejected or expired); sets the pin |
| `pinned_fallback` | Fallback: this browser got a fallback during an outage in this session |
| `unknown_product` | Fallback: the config has no such product |
| `currency_mismatch` | Fallback: the config's currency differs from your guard |
| `out_of_bounds` | Fallback: a price is outside the product's signed range |

| Method | |
|---|---|
| `log_exposure(quote)` | Logs a manual-mode exposure; returns `True` if one was queued |
| `track_purchase(*, provider, order_id, amount, currency, metadata=None, timestamp=None)` | Reads `pp_exp`, `pp_var`, `pp_vid`, `pp_sig` from `metadata`; sends right away |
| `track_refund(*, provider, order_id, currency, refund_id=None, amount=None, refunded_total=None, timestamp=None)` | Sends right away |
| `flush()` | Sends queued events and waits (at most 500 per request; purchases and refunds are kept on failure) |
| `refresh()` / `preload()` | Fetch the config now / only if none is usable |
| `close()` | Flushes and stops the flush timer |

Providers: `stripe`, `polar`, `lemonsqueezy`, `dodo`, `paddle`, `creem`, `other`. Timestamps are milliseconds, Unix seconds or a `datetime`. None of these methods raise.

| Option | Default | |
|---|---|---|
| `secret_key` | required | `pp_sk_live_…` or `pp_sk_test_…` |
| `products` | required | Product key → `{currency, fallback_price}` or `ProductGuard` |
| `disabled` | `False` | Kill switch |
| `api_url` | `https://api.leverprice.com` | |
| `timeout_ms` | `500` | Longest wait for a config when none is usable |
| `refresh_interval_ms` | `30000` | Config revalidation period |
| `max_stale_ms` | `86400000` | Stop using a config this long after the server last confirmed it |
| `cookie_name` | `"__pp"` | The outage pin is `<cookie_name>_f` |
| `cookie_max_age_seconds` | `15552000` | 180 days |
| `cookie_secure` | `True` | Set `False` only for plain-HTTP development |
| `cookie_domain` | `None` | |
| `country_header` | `None` | Request header your CDN or host sets to the visitor's country; overrides **Project settings → Visitor country**. A header every browser sends (`accept-language`, `x-forwarded-for`…) is ignored and reported to `on_error`. See [Regional prices](#regional-prices) |
| `local_currencies` | `False` | Serve regional prices in the visitor's currency, not only in each product's own. Set it only when your checkout charges `quote.price` in `quote.currency` |
| `flush_interval_ms` | `2000` | Longest time exposures wait before being sent |
| `trusted_root_keys` | `ROOT_PUBLIC_KEYS` | For staging and local development |
| `transport` | `UrllibTransport()` | Any object with `request(method, url, headers, body, timeout)` |
| `on_error` | log a warning | Receives every SDK error; exceptions it raises are ignored |

Errors are logged on the `leverprice` logger when no `on_error` is given.

**Upgrading from 0.3:** no code changes. `min_price` and `max_price` in your code are no longer used: the product's range set in Leverprice applies, and you can delete them whenever you like. If the limits in your code were tighter than that range, tighten the range in Leverprice before you upgrade. Regional prices need no code: turn them on in the dashboard and set **Project settings → Visitor country**. Add `local_currencies=True` if your checkout passes `quote.currency` and you want prices in visitors' own currencies.

## License

MIT
