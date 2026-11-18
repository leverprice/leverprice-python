from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from leverprice import Leverprice, TransportResponse
from leverprice import _ed25519 as ed

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
SECRET = "pp_sk_test_" + "A1b2C3d4E5" * 4
PIN = "__pp_f=1; Path=/; Max-Age=1800; SameSite=Lax; HttpOnly; Secure"

QUOTE_GUARDS: Dict[str, Dict[str, Any]] = {
    "pro": {"currency": "usd", "fallback_price": 1900},
    "basic": {"currency": "usd", "fallback_price": 1000},
    "euro": {"currency": "eur", "fallback_price": 1200},
    "wild": {"currency": "usd", "fallback_price": 2000},
    "bad": {"currency": "usd", "fallback_price": 150},
    "ghost": {"currency": "usd", "fallback_price": 4900},
    "geo": {"currency": "usd", "fallback_price": 2900},
    "geox": {"currency": "usd", "fallback_price": 2900},
    "badgeo": {"currency": "usd", "fallback_price": 2900},
    "badgeox": {"currency": "usd", "fallback_price": 2900},
}


def find_vectors() -> Optional[Path]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "conformance" / "vectors.json"
        if candidate.is_file():
            return candidate
    return None


def load_vectors() -> Dict[str, Any]:
    path = find_vectors()
    if path is None:
        if os.environ.get("LEVERPRICE_REQUIRE_VECTORS") == "1":
            pytest.fail("conformance/vectors.json not found")
        pytest.skip("conformance/vectors.json not found", allow_module_level=True)
    with path.open(encoding="utf-8") as fh:
        data: Dict[str, Any] = json.load(fh)
    return data


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret_scalar(seed: bytes) -> Tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _secret_scalar(seed)
    return ed.encode_point(ed.double_scalarmult(a, 0, ed.IDENTITY))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _secret_scalar(seed)
    pub = public_key(seed)
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed.L
    r_bytes = ed.encode_point(ed.double_scalarmult(r, 0, ed.IDENTITY))
    k = int.from_bytes(hashlib.sha512(r_bytes + pub + message).digest(), "little") % ed.L
    s = (r + k * a) % ed.L
    return r_bytes + s.to_bytes(32, "little")


@dataclass
class SigningKeys:
    root: bytes = hashlib.sha256(b"root").digest()
    signers: Dict[str, bytes] = field(
        default_factory=lambda: {"k1": hashlib.sha256(b"k1").digest(), "k2": hashlib.sha256(b"k2").digest()}
    )

    @property
    def root_keys(self) -> Dict[str, str]:
        return {"r1": b64url(public_key(self.root))}

    def envelope(
        self,
        payload: Dict[str, Any],
        *,
        key_id: str = "k1",
        keyset_version: int = 1,
        keyset_keys: Tuple[str, ...] = ("k1",),
        keyset_expires_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        keyset = {
            "schema": 1,
            "version": keyset_version,
            "issuedAt": 0,
            "expiresAt": keyset_expires_at,
            "keys": [{"id": k, "publicKey": b64url(public_key(self.signers[k]))} for k in keyset_keys],
        }
        keyset_payload = b64url(json.dumps(keyset).encode())
        config_payload = b64url(json.dumps(payload).encode())
        return {
            "payload": config_payload,
            "signature": b64url(sign(self.signers[key_id], config_payload.encode())),
            "keyId": key_id,
            "keyset": {
                "payload": keyset_payload,
                "signature": b64url(sign(self.root, keyset_payload.encode())),
                "rootKeyId": "r1",
            },
        }


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


ASSIGNMENT_KEY = b64url(hashlib.sha256(b"assignment").digest())


def config_payload(
    version: int = 5,
    *,
    products: Optional[Dict[str, Any]] = None,
    expires_at: float = 4102444800000,
    project_id: str = "proj_test",
    secret: str = SECRET,
) -> Dict[str, Any]:
    return {
        "schema": 2,
        "projectId": project_id,
        "version": version,
        "issuedAt": 0,
        "expiresAt": expires_at,
        "assignmentKey": ASSIGNMENT_KEY,
        "keyFingerprints": [fingerprint(secret)],
        "products": products if products is not None else default_products(),
    }


def default_products(base_price: int = 2400) -> Dict[str, Any]:
    return {
        "pro": {
            "id": "prod_pro",
            "currency": "usd",
            "price": base_price,
            "minPrice": 1000,
            "maxPrice": 5000,
            "experiment": {
                "id": "exp_pro",
                "salt": "s1",
                "variants": [{"id": "a", "price": 1900, "weight": 5000}, {"id": "b", "price": 2900, "weight": 5000}],
            },
        },
        "team": {
            "id": "prod_team",
            "currency": "usd",
            "price": 4900,
            "minPrice": 1000,
            "maxPrice": 9000,
            "experiment": {
                "id": "exp_team",
                "salt": "s2",
                "variants": [{"id": "c", "price": 4900, "weight": 5000}, {"id": "d", "price": 5900, "weight": 5000}],
            },
        },
        "basic": {"id": "prod_basic", "currency": "usd", "price": base_price // 2, "minPrice": 1, "maxPrice": 9000},
    }


GUARDS: Dict[str, Dict[str, Any]] = {
    "pro": {"currency": "usd", "fallback_price": 1900},
    "team": {"currency": "usd", "fallback_price": 4900},
    "basic": {"currency": "usd", "fallback_price": 1000},
}

REGIONS: List[Dict[str, Any]] = [
    {"countries": ["BR", "MX"], "currency": "usd", "price": 2000},
    {"countries": ["IN"], "currency": "INR", "price": 99_900},
]


def regional_products() -> Dict[str, Any]:
    geo = {
        "id": "prod_geo",
        "currency": "usd",
        "price": 2900,
        "minPrice": 1900,
        "maxPrice": 4900,
        "checkoutUrl": "https://buy.example.com/geo",
        "regions": REGIONS,
    }
    experiment = {
        "id": "exp_geo",
        "salt": "s3",
        "variants": [
            {"id": "a", "price": 1900, "weight": 5000, "regional": [1300, 64_900]},
            {"id": "b", "price": 3900, "weight": 5000, "regional": [2700, 129_900]},
        ],
    }
    return {"geo": geo, "geox": {**geo, "id": "prod_geox", "experiment": experiment}}


REGIONAL_GUARDS: Dict[str, Dict[str, Any]] = {
    key: {"currency": "usd", "fallback_price": 2900} for key in ("geo", "geox")
}


def country_test_products() -> Dict[str, Any]:
    """Tests where every visitor gets variant ``a``, except visitors from India, who all get ``b``."""
    product = {"currency": "usd", "price": 2900, "minPrice": 1900, "maxPrice": 4900}
    test = {
        "salt": "s4",
        "variants": [{"id": "a", "price": 1900, "weight": 10_000}, {"id": "b", "price": 3900, "weight": 0}],
        "splits": [{"countries": ["IN"], "weights": [0, 10_000]}],
    }
    scoped = {**test, "id": "exp_local", "countries": ["BR", "IN"]}
    return {
        "local": {**product, "id": "prod_local", "scopedExperiment": scoped},
        "split": {**product, "id": "prod_split", "experiment": {**test, "id": "exp_split"}},
    }


COUNTRY_TEST_GUARDS: Dict[str, Dict[str, Any]] = {
    key: {"currency": "usd", "fallback_price": 2900} for key in ("local", "split")
}


@dataclass
class Call:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]


Handler = Callable[[Call], TransportResponse]


class FakeServer:
    def __init__(self, envelope: Optional[Dict[str, Any]] = None, etag: str = '"v5"') -> None:
        self.envelope = envelope
        self.etag = etag
        self.calls: List[Call] = []
        self.batches: List[Dict[str, Any]] = []
        self.config_handler: Handler = self._serve_config
        self.events_handler: Handler = lambda call: TransportResponse(202, {}, b'{"accepted":1}')
        self._lock = threading.Lock()

    def _serve_config(self, call: Call) -> TransportResponse:
        return TransportResponse(200, {"ETag": self.etag}, json.dumps(self.envelope))

    def __call__(
        self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float
    ) -> TransportResponse:
        call = Call(method, url, dict(headers), body)
        with self._lock:
            self.calls.append(call)
        if url.endswith("/v1/config"):
            return self.config_handler(call)
        with self._lock:
            self.batches.append(json.loads(body or b"null"))
        return self.events_handler(call)

    @property
    def config_calls(self) -> List[Call]:
        return [c for c in self.calls if c.url.endswith("/v1/config")]

    @property
    def event_calls(self) -> List[Call]:
        return [c for c in self.calls if c.url.endswith("/v1/events")]

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [e for b in self.batches for e in b["events"]]


class Clock:
    def __init__(self, now: int = 1_800_000_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def wait_idle(lp: Leverprice, timeout: float = 3.0) -> None:
    assert wait_for(lambda: lp._inflight is None, timeout)


class Headers:
    """A request object exposing ``headers`` like most frameworks."""

    def __init__(self, headers: Dict[str, str]) -> None:
        self.headers = headers


def make_client(server: FakeServer, keys: Optional[SigningKeys] = None, **options: Any) -> Leverprice:
    errors: List[Exception] = options.pop("errors", [])
    options.setdefault("timeout_ms", 5_000)
    options.setdefault("on_error", errors.append)
    lp = Leverprice(
        options.pop("secret_key", SECRET),
        options.pop("products", GUARDS),
        trusted_root_keys=(keys or SigningKeys()).root_keys,
        transport=server,
        **options,
    )
    lp._retry_delays = (0.001, 0.001)
    return lp


def decode_cookie(set_cookie: str) -> Dict[str, Any]:
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    body = value.split(".", 1)[0]
    data: Dict[str, Any] = json.loads(b64url_decode(body))
    return data
