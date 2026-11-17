# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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

[0.3.0]: https://github.com/leverprice/leverprice-python/releases/tag/v0.3.0
