from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._util import (
    JS_WHITESPACE,
    LeverpriceError,
    as_number,
    from_b64url,
    from_utf8,
    is_id,
    js_equals,
    json_loads,
    safe_int,
)

PROTOCOL_VERSION = 2
CONFIG_VERSION_HEADER = "leverprice-config-version"
MAX_EVENTS_PER_REQUEST = 500
MAX_MINOR_UNITS = 10_000_000_000
PROVIDERS = ("stripe", "polar", "lemonsqueezy", "dodo", "other")

_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
_WIRE_CURRENCY_RE = re.compile(r"[a-z]{3}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{16}")
_HTTPS_RE = re.compile(r"https://[^" + re.escape(JS_WHITESPACE) + r"]+", re.IGNORECASE)


@dataclass(frozen=True)
class ConfigVariant:
    id: str
    price: int
    weight: int
    checkout_url: Optional[str] = None


@dataclass(frozen=True)
class ConfigExperiment:
    id: str
    salt: str
    variants: Tuple[ConfigVariant, ...]


@dataclass(frozen=True)
class ConfigProduct:
    id: str
    currency: str
    price: int
    min_price: int
    max_price: int
    checkout_url: Optional[str] = None
    experiment: Optional[ConfigExperiment] = None


@dataclass(frozen=True)
class Config:
    project_id: str
    version: int
    issued_at: float
    expires_at: float
    assignment_key: str
    key_fingerprints: Tuple[str, ...]
    products: Dict[str, ConfigProduct] = field(default_factory=dict)


def parse_config(payload: str) -> Tuple[Config, List[str]]:
    try:
        raw = json_loads(from_utf8(from_b64url(payload)))
    except Exception:
        raise LeverpriceError("Leverprice config: payload is not base64url JSON") from None
    if not isinstance(raw, dict):
        raise LeverpriceError("Leverprice config: payload is not an object")
    schema = raw.get("schema")
    if not js_equals(schema, PROTOCOL_VERSION):
        raise LeverpriceError(
            f"Leverprice config: unsupported schema {_js_string(schema)} (this SDK speaks schema {PROTOCOL_VERSION})"
        )
    fingerprints = raw.get("keyFingerprints")
    if not isinstance(fingerprints, list) or not all(
        isinstance(f, str) and _FINGERPRINT_RE.fullmatch(f) for f in fingerprints
    ):
        raise LeverpriceError("Leverprice config: bad keyFingerprints")
    project_id = raw.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        raise LeverpriceError("Leverprice config: no projectId")
    version = safe_int(raw.get("version"))
    if version is None or version < 0:
        raise LeverpriceError("Leverprice config: bad version")
    expires_at = as_number(raw.get("expiresAt"))
    if expires_at is None or not math.isfinite(expires_at):
        raise LeverpriceError("Leverprice config: bad expiresAt")
    assignment_key = raw.get("assignmentKey")
    if not isinstance(assignment_key, str) or _decoded_length(assignment_key) < 32:
        raise LeverpriceError("Leverprice config: assignmentKey must be at least 32 bytes of base64url")
    products = raw.get("products")
    if not isinstance(products, dict):
        raise LeverpriceError("Leverprice config: no products")

    parsed: Dict[str, ConfigProduct] = {}
    dropped: List[str] = []
    for key, value in products.items():
        product = _parse_product(value)
        if product is None:
            dropped.append(key)
        else:
            parsed[key] = product
    config = Config(
        project_id=project_id,
        version=version,
        issued_at=as_number(raw.get("issuedAt")) or 0.0,
        expires_at=expires_at,
        assignment_key=assignment_key,
        key_fingerprints=tuple(fingerprints),
        products=parsed,
    )
    return config, dropped


def _parse_product(p: Any) -> Optional[ConfigProduct]:
    if not isinstance(p, dict):
        return None
    currency = p.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        return None
    price, min_price, max_price = (safe_int(p.get(k)) for k in ("price", "minPrice", "maxPrice"))
    if price is None or min_price is None or max_price is None:
        return None
    experiment: Optional[ConfigExperiment] = None
    e = p.get("experiment")
    if e is not None:
        if not isinstance(e, dict) or not is_id(e.get("id")) or not isinstance(e.get("salt"), str):
            return None
        raw_variants = e.get("variants")
        if not isinstance(raw_variants, list):
            return None
        variants = []
        for v in raw_variants:
            if not isinstance(v, dict) or not is_id(v.get("id")):
                return None
            v_price, weight = safe_int(v.get("price")), safe_int(v.get("weight"))
            if v_price is None or weight is None or weight < 0:
                return None
            variants.append(ConfigVariant(v["id"], v_price, weight, _https_url(v.get("checkoutUrl"))))
        experiment = ConfigExperiment(e["id"], e["salt"], tuple(variants))
    product_id = p.get("id")
    return ConfigProduct(
        id=product_id if isinstance(product_id, str) else "",
        currency=currency.lower(),
        price=price,
        min_price=min_price,
        max_price=max_price,
        checkout_url=_https_url(p.get("checkoutUrl")),
        experiment=experiment,
    )


def _https_url(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _HTTPS_RE.fullmatch(v) else None


def _decoded_length(b64: str) -> int:
    try:
        return len(from_b64url(b64))
    except ValueError:
        return 0


def _js_string(v: Any) -> str:
    if v is None:
        return "undefined"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


def _is_minor_units(v: Any) -> bool:
    n = safe_int(v)
    return n is not None and 0 <= n <= MAX_MINOR_UNITS


def _is_text(v: Any) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= 255


def _valid_attribution(a: Any) -> bool:
    if not isinstance(a, dict):
        return False
    sig = a.get("signature")
    return (
        is_id(a.get("experimentId"))
        and is_id(a.get("variantId"))
        and is_id(a.get("visitorId"))
        and (sig is None or (isinstance(sig, str) and _SIGNATURE_RE.fullmatch(sig) is not None))
    )


def event_problem(e: Mapping[str, Any]) -> Optional[str]:
    ts = safe_int(e.get("timestamp"))
    if ts is None or ts <= 0:
        return "timestamp must be a positive integer (ms)"
    kind = e.get("type")
    if kind == "exposure":
        ids = (e.get("experimentId"), e.get("variantId"), e.get("visitorId"))
        return None if all(is_id(i) for i in ids) else "invalid experiment/variant/visitor id"
    provider = e.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        return f'unknown provider "{_js_string(provider)}"'
    if not _is_text(e.get("orderId")):
        return "orderId must be a string of 1–255 characters"
    currency = e.get("currency")
    if not isinstance(currency, str) or not _WIRE_CURRENCY_RE.fullmatch(currency):
        return "currency must be a 3-letter ISO 4217 code"
    if kind == "purchase":
        if not _is_minor_units(e.get("amount")):
            return "amount must be a non-negative integer in minor units"
        attribution = e.get("attribution")
        if attribution is not None and not _valid_attribution(attribution):
            return "invalid attribution"
        return None
    if kind != "refund":
        return f'unknown event type "{_js_string(kind)}"'
    has_refund_id, has_amount, has_total = "refundId" in e, "amount" in e, "refundedTotal" in e
    if has_refund_id and not _is_text(e.get("refundId")):
        return "refundId must be a string of 1–255 characters"
    if has_amount and not _is_minor_units(e.get("amount")):
        return "amount must be a non-negative integer"
    if has_total and not _is_minor_units(e.get("refundedTotal")):
        return "refundedTotal must be a non-negative integer"
    if not (has_refund_id and has_amount) and not has_total:
        return "refund needs refundId + amount, or refundedTotal"
    return None
