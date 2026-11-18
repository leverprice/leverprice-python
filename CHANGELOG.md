# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-11-18

### Added

- VPN guard: a visitor keeps the price of a country that pays more, where they were seen in the last 7 days, so a VPN to a cheaper country no longer lowers the price. A visitor who moves to a dearer country pays its price from then on. The held country is `quote.country` and lives in the signed `__pp` cookie, which a regional product now sets even without a test. Turned on by the signed config; older SDKs ignore it.
- A visitor who refuses cookies keeps one price and counts once: without a cookie, the visitor is keyed by an HMAC of their IP address (IPv6 by its /64), browser and languages, made with the project's secret. Neither leaves your server.
- Every config request says which products your code prices (`Leverprice-Products`) and why the last config was refused, if it was (`Leverprice-Config-Error`), so the dashboard's Setup page turns green only when visitors get real prices and otherwise says what to fix.

### Changed

- `country_header` naming a header every browser sends (`Accept-Language`, `X-Forwarded-For`…) is ignored and reported to `on_error`, and such a header in the project settings is never read: visitors can no longer pick their own country.

### Fixed

- A cold start no longer gives up early: each request waits its own `timeout_ms` for the first config, even when `preload()` in another thread or another request started the fetch earlier. Before, a busy server answered the first visitors with the fallback early, said nothing, and pinned them to it for 30 minutes. A request that does give up now reports `Leverprice config: still loading after 500 ms (timeout_ms); …` to `on_error`, once per fetch.

## [0.4.0] - 2026-11-18

Cookies and the wire protocol are compatible with 0.3.

### Changed

- Limits set in code (`min_price`, `max_price`) no longer hold prices: the product's range set in Leverprice applies. If your limits were tighter than that range, tighten it in the dashboard before you upgrade.

### Added

- Settings live in Leverprice, not in code: the product's price range and the header that carries the visitor's country (Project settings → Visitor country) arrive in the signed config. `min_price` and `max_price` in code are no longer used; 0.3 code that sets them keeps running.
- Regional prices: a product can cost something else in each country, in your currency or the visitor's. `country` argument of `get_price` and `get_price_async`, `country` / `regional` on the quote, and a `country_header` option that overrides the project setting. Prices in the visitor's currency need `local_currencies=True` (set it when your checkout charges `quote.currency`); `quote.currency` is then the region's currency.
- Exposures carry the visitor's country when it is known.
- Tests by country: a test can run in selected countries only, and countries can be split between the prices their own way. Both are set in the dashboard; the SDK only needs the visitor's country.
- `track_purchase()` and `track_refund()` accept the providers `paddle` and `creem`.

### Fixed

- A request arriving at the moment a config fetch completed could get the fallback price with reason `no_config`; it now gets the config that was just loaded.
- Turbo 8 hover prefetches (`X-Sec-Purpose: prefetch`) are priced but no longer counted as exposures, like other prefetches.
- `track_purchase` reads attribution from Stripe objects, which are no longer dicts since stripe-python 15. Before, `metadata=session.metadata` sent the purchase without attribution.
- README: the Stripe example uses `StripeClient` and works with stripe-python 15 and later. The webhook counts only paid Checkout Sessions, handles `checkout.session.async_payment_succeeded` and reports a subscription's first payment by its PaymentIntent id (as verified webhooks do) and skips $0 sessions.

## [0.3.0] - 2026-11-17

First release of the Python SDK, implementing protocol v2 of the Leverprice server SDK specification.

### Added

- `Leverprice` client with `get_price`, `log_exposure`, `track_purchase`, `track_refund`, `flush`, `refresh`, `preload` and `close`, plus `_async` variants for asyncio.
- Signed config trust chain (root → keyset → config) with key revocation, rollback protection and expiry.
- Ed25519 verification through `cryptography` when installed, with a bundled pure-Python RFC 8032 verifier as fallback.
- Request adapters for Django, Flask/Werkzeug, Starlette/FastAPI, aiohttp, WSGI environ, ASGI scope and plain header maps.
- `apply_cookies` helper for Django, Flask/Werkzeug, Starlette/FastAPI and aiohttp responses.
- Kill switch through `disabled=True` or `LEVERPRICE_DISABLED`.
- Fork safety for pre-forking servers.

[0.4.0]: https://github.com/leverprice/leverprice-python/releases/tag/v0.4.0
[0.3.0]: https://github.com/leverprice/leverprice-python/releases/tag/v0.3.0
