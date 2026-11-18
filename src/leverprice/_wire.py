from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

from ._util import (
    COUNTRY_RE,
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
MAX_REGIONS = 300
MAX_SPLITS = 300
MAX_SCOPE = 300
PROVIDERS = ("stripe", "polar", "lemonsqueezy", "dodo", "paddle", "creem", "other")

_CURRENCY_RE = re.compile(r"[A-Za-z]{3}")
_WIRE_CURRENCY_RE = re.compile(r"[a-z]{3}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{16}")
_HTTPS_RE = re.compile(r"https://[^" + re.escape(JS_WHITESPACE) + r"]+", re.IGNORECASE)
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


@dataclass(frozen=True)
class ConfigVariant:
    id: str
    price: int
    weight: int
    checkout_url: Optional[str] = None
    regional: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ConfigExperiment:
    id: str
    salt: str
    variants: Tuple[ConfigVariant, ...]
    scope: Optional[FrozenSet[str]] = None
    split_of: Dict[str, Tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigRegion:
    countries: Tuple[str, ...]
    currency: str
    price: int


@dataclass(frozen=True)
class ConfigProduct:
    id: str
    currency: str
    price: int
    min_price: int
    max_price: int
    checkout_url: Optional[str] = None
    experiment: Optional[ConfigExperiment] = None
    regions: Tuple[ConfigRegion, ...] = ()
    region_of: Dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    project_id: str
    version: int
    issued_at: float
    expires_at: float
    assignment_key: str
    key_fingerprints: Tuple[str, ...]
    country_header: Optional[str] = None
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
        country_header=_header_name(raw.get("countryHeader")),
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
    regions = _parse_regions(p.get("regions"))
    if regions is None:
        return None
    region_of: Dict[str, int] = {}
    for index, region in enumerate(regions):
        for country in region.countries:
            if country in region_of:
                return None
            region_of[country] = index
    open_test, scoped_test = p.get("experiment"), p.get("scopedExperiment")
    if open_test is not None and scoped_test is not None:
        return None
    raw, scoped = (open_test, False) if scoped_test is None else (scoped_test, True)
    experiment: Optional[ConfigExperiment] = None
    if raw is not None:
        experiment = _parse_experiment(raw, len(regions), scoped)
        if experiment is None:
            return None
    product_id = p.get("id")
    return ConfigProduct(
        id=product_id if isinstance(product_id, str) else "",
        currency=currency.lower(),
        price=price,
        min_price=min_price,
        max_price=max_price,
        checkout_url=_https_url(p.get("checkoutUrl")),
        experiment=experiment,
        regions=regions,
        region_of=region_of,
    )


def _parse_regions(raw: Any) -> Optional[Tuple[ConfigRegion, ...]]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_REGIONS:
        return None
    regions = []
    for r in raw:
        if not isinstance(r, dict):
            return None
        countries = _country_list(r.get("countries"))
        currency, price = r.get("currency"), _positive_int(r.get("price"))
        if countries is None or price is None:
            return None
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            return None
        regions.append(ConfigRegion(tuple(countries), currency.lower(), price))
    return tuple(regions)


def _parse_experiment(e: Any, regions: int, scoped: bool) -> Optional[ConfigExperiment]:
    if not isinstance(e, dict) or not is_id(e.get("id")) or not isinstance(e.get("salt"), str):
        return None
    raw_variants = e.get("variants")
    if not isinstance(raw_variants, list):
        return None
    variants = []
    for v in raw_variants:
        if not isinstance(v, dict) or not is_id(v.get("id")):
            return None
        price, weight = safe_int(v.get("price")), _weight(v.get("weight"))
        if price is None or weight is None:
            return None
        regional = _parse_ints(v.get("regional"), regions, _positive_int) if regions else ()
        if regional is None:
            return None
        variants.append(ConfigVariant(v["id"], price, weight, _https_url(v.get("checkoutUrl")), regional))
    split_of = _parse_splits(e.get("splits"), len(variants))
    if split_of is None:
        return None
    scope: Optional[FrozenSet[str]] = None
    if scoped:
        countries = _country_list(e.get("countries"))
        if countries is None or len(countries) > MAX_SCOPE:
            return None
        scope = frozenset(countries)
        if len(scope) != len(countries):
            return None
    return ConfigExperiment(e["id"], e["salt"], tuple(variants), scope, split_of)


def _parse_splits(raw: Any, variants: int) -> Optional[Dict[str, Tuple[int, ...]]]:
    split_of: Dict[str, Tuple[int, ...]] = {}
    if raw is None:
        return split_of
    if not isinstance(raw, list) or len(raw) > MAX_SPLITS:
        return None
    for s in raw:
        if not isinstance(s, dict):
            return None
        countries, weights = _country_list(s.get("countries")), _parse_ints(s.get("weights"), variants, _weight)
        if countries is None or weights is None:
            return None
        for country in countries:
            if country in split_of:
                return None
            split_of[country] = weights
    return split_of


def _country_list(v: Any) -> Optional[List[str]]:
    if isinstance(v, list) and v and all(isinstance(c, str) and COUNTRY_RE.fullmatch(c) for c in v):
        return v
    return None


def _parse_ints(raw: Any, count: int, parse: Callable[[Any], Optional[int]]) -> Optional[Tuple[int, ...]]:
    if not isinstance(raw, list) or len(raw) != count:
        return None
    values = []
    for v in raw:
        value = parse(v)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _positive_int(v: Any) -> Optional[int]:
    n = safe_int(v)
    return n if n is not None and n > 0 else None


def _weight(v: Any) -> Optional[int]:
    n = safe_int(v)
    return n if n is not None and n >= 0 else None


def _https_url(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _HTTPS_RE.fullmatch(v) else None


def _header_name(v: Any) -> Optional[str]:
    return v.lower() if isinstance(v, str) and _HEADER_NAME_RE.fullmatch(v) else None


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
        if not all(is_id(i) for i in ids):
            return "invalid experiment/variant/visitor id"
        country = e.get("country")
        if "country" in e and not (isinstance(country, str) and COUNTRY_RE.fullmatch(country)):
            return "country must be an ISO 3166-1 alpha-2 code"
        return None
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
