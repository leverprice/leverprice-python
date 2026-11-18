from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from _support import QUOTE_GUARDS, FakeServer, Headers, load_vectors
from leverprice import Leverprice, LeverpriceError, is_bot
from leverprice._client import _bucket, _pick_variant
from leverprice._cookie import AssignmentState, assign, mark_exposed, set_cookie_header, sign_assignment, state_json
from leverprice._cookie import verify_assignment as verify_cookie
from leverprice._request import cookie_values, is_prefetch, normalize_country
from leverprice._trust import TrustChain, accept_config, key_fingerprint
from leverprice._util import from_b64url, hmac_hex
from leverprice._wire import Config, ConfigVariant, event_problem

VECTORS = load_vectors()
KEY = from_b64url(VECTORS["assignmentKey"])


def _state(raw: Dict[str, Any]) -> AssignmentState:
    return AssignmentState(raw["v"], dict(raw["a"]), list(raw["x"]))


def _plain(state: Optional[AssignmentState]) -> Optional[Dict[str, Any]]:
    if state is None:
        return None
    return {"v": state.v, "a": [[k, v] for k, v in state.a.items()], "x": state.x}


def test_key_fingerprints() -> None:
    for case in VECTORS["keyFingerprints"]:
        assert key_fingerprint(case["secretKey"]) == case["fingerprint"]


@pytest.mark.parametrize("case", VECTORS["trust"], ids=lambda c: c["name"])
def test_trust(case: Dict[str, Any], ed25519_backend: str) -> None:
    chain = TrustChain(VECTORS["rootKeys"], case.get("minKeysetVersion", 1))
    fingerprint = key_fingerprint(VECTORS["secretKey"])
    current: Optional[Config] = None
    for step in case["steps"]:
        now = step.get("now", VECTORS["now"])
        if step["expect"] == "reject":
            with pytest.raises(LeverpriceError):
                accept_config(chain, fingerprint, current, step["envelope"], now)
            continue
        verified = accept_config(chain, fingerprint, current, step["envelope"], now)
        assert verified.config.version == step["version"]
        assert verified.dropped == step["dropped"]
        current = verified.config


@pytest.mark.parametrize("case", VECTORS["visitorIds"], ids=lambda c: c["visitorKey"])
def test_visitor_ids(case: Dict[str, Any]) -> None:
    assert case["message"] == "pp:visitor:v1:" + case["visitorKey"]
    assert hmac_hex(KEY, case["message"], 32) == case["visitorId"]


@pytest.mark.parametrize("case", VECTORS["attribution"], ids=lambda c: c["visitorId"])
def test_attribution(case: Dict[str, Any]) -> None:
    message = f"pp:attr:v1:{case['experimentId']}:{case['variantId']}:{case['visitorId']}"
    assert message == case["message"]
    assert hmac_hex(KEY, message, 16) == case["signature"]


@pytest.mark.parametrize("case", VECTORS["buckets"], ids=lambda c: f"{c['salt']}:{c['visitorId']}")
def test_buckets(case: Dict[str, Any]) -> None:
    assert _bucket(case["salt"], case["visitorId"]) == case["bucket"]


@pytest.mark.parametrize("case", VECTORS["pickVariant"], ids=lambda c: f"{c['weights']}@{c['bucket']}")
def test_pick_variant(case: Dict[str, Any]) -> None:
    variants = tuple(ConfigVariant(f"v{i}", 100, w) for i, w in enumerate(case["weights"]))
    assert variants.index(_pick_variant(variants, case["bucket"])) == case["index"]
    unweighted = tuple(ConfigVariant(v.id, v.price, 0) for v in variants)
    assert unweighted.index(_pick_variant(unweighted, case["bucket"], tuple(case["weights"]))) == case["index"]


@pytest.mark.parametrize("case", VECTORS["cookies"]["sign"], ids=lambda c: c["json"])
def test_cookie_sign(case: Dict[str, Any]) -> None:
    state = _state(case["state"])
    assert state_json(state) == case["json"]
    assert sign_assignment(KEY, state) == case["cookie"]


@pytest.mark.parametrize("case", VECTORS["cookies"]["verify"], ids=lambda c: c["name"])
def test_cookie_verify(case: Dict[str, Any]) -> None:
    assert _plain(verify_cookie(KEY, case["cookie"])) == case["state"]


@pytest.mark.parametrize("case", VECTORS["cookies"]["ops"], ids=lambda c: c["name"])
def test_cookie_ops(case: Dict[str, Any]) -> None:
    state = _state(case["state"])
    op = case["op"]
    if "assign" in op:
        assign(state, *op["assign"])
    else:
        assert mark_exposed(state, op["markExposed"]) is case["changed"]
    assert _plain(state) == case["result"]


@pytest.mark.parametrize("case", VECTORS["cookieHeader"], ids=lambda c: c["header"])
def test_cookie_header(case: Dict[str, Any]) -> None:
    assert cookie_values(case["header"], case["name"]) == case["values"]


@pytest.mark.parametrize("case", VECTORS["prefetch"], ids=lambda c: json.dumps(c["headers"]))
def test_prefetch(case: Dict[str, Any]) -> None:
    assert is_prefetch(case["headers"]) is case["prefetch"]
    upper = {k.upper(): v for k, v in case["headers"].items()}
    assert is_prefetch(upper) is case["prefetch"]


@pytest.mark.parametrize("case", VECTORS["setCookie"], ids=lambda c: c["header"][:40])
def test_set_cookie(case: Dict[str, Any]) -> None:
    header = set_cookie_header(case["name"], case["value"], case["maxAge"], case["secure"], case["domain"])
    assert header == case["header"]


@pytest.mark.parametrize("case", VECTORS["countries"], ids=lambda c: repr(c["value"]))
def test_countries(case: Dict[str, Any]) -> None:
    assert normalize_country(case["value"]) == case["country"]


@pytest.mark.parametrize("case", VECTORS["bots"], ids=lambda c: c["userAgent"][:60])
def test_bots(case: Dict[str, Any]) -> None:
    assert is_bot(case["userAgent"]) is case["bot"]


@pytest.mark.parametrize("case", VECTORS["events"], ids=lambda c: json.dumps(c["event"])[:80])
def test_events(case: Dict[str, Any]) -> None:
    assert (event_problem(case["event"]) is None) is case["valid"]


_QUOTE_FIELDS = {
    "price": "price",
    "currency": "currency",
    "country": "country",
    "regional": "regional",
    "isFallback": "is_fallback",
    "reason": "reason",
    "experimentId": "experiment_id",
    "variantId": "variant_id",
    "visitorId": "visitor_id",
    "metadata": "metadata",
    "checkoutUrl": "checkout_url",
    "exposureLogged": "exposure_logged",
    "setCookie": "set_cookie",
}


@pytest.mark.parametrize("form", ["dict", "object"])
@pytest.mark.parametrize("case", VECTORS["quotes"], ids=lambda c: c["name"])
def test_quotes(case: Dict[str, Any], form: str, ed25519_backend: str) -> None:
    server = FakeServer(VECTORS["config"])
    errors: List[Exception] = []
    guards = {**QUOTE_GUARDS, **{k: _snake_guard(g) for k, g in case.get("guards", {}).items()}}
    lp = Leverprice(
        VECTORS["secretKey"],
        guards,
        trusted_root_keys=VECTORS["rootKeys"],
        transport=server,
        on_error=errors.append,
        timeout_ms=10_000,
        country_header=case.get("countryHeader"),
        local_currencies=case.get("localCurrencies", False),
    )
    request: Any = dict(case["headers"]) if form == "dict" else Headers(dict(case["headers"]))
    options: Dict[str, Any] = {"user_id": case["userId"]}
    for name in ("exposure", "bot", "country"):
        if name in case:
            options[name] = case[name]
    quote = lp.get_price(case["productKey"], request, **options)
    lp.flush()

    expect = case["expect"]
    assert quote.product_key == case["productKey"]
    assert set(expect) == {*_QUOTE_FIELDS, "exposures"}
    for wire, attr in _QUOTE_FIELDS.items():
        assert getattr(quote, attr) == expect[wire], attr
    sent = [{k: v for k, v in e.items() if k != "timestamp"} for e in server.events]
    assert sent == expect["exposures"]
    assert [str(e) for e in errors if "malformed products" not in str(e)] == case["errors"]
    lp.close()


_SNAKE = {"fallbackPrice": "fallback_price", "minPrice": "min_price", "maxPrice": "max_price"}


def _snake_guard(guard: Dict[str, Any]) -> Dict[str, Any]:
    return {_SNAKE.get(k, k): v for k, v in guard.items()}
