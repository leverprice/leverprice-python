from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from decimal import Decimal
from typing import Any, Optional

MAX_SAFE_INTEGER = 2**53 - 1

JS_WHITESPACE = "\t\n\x0b\x0c\r                  　﻿"

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITS_RE = re.compile(r"[0-9]+")
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class LeverpriceError(Exception):
    """An error reported by the SDK through ``on_error``."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def js_trim(s: str) -> str:
    return s.strip(JS_WHITESPACE)


def is_id(v: Any) -> bool:
    return isinstance(v, str) and ID_RE.fullmatch(v) is not None


def utf8(s: str) -> bytes:
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:
        # TextEncoder semantics: lone surrogates become U+FFFD.
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def from_utf8(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def from_b64url(s: str) -> bytes:
    if not isinstance(s, str) or _B64URL_RE.fullmatch(s) is None or len(s) % 4 == 1:
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError) as err:
        raise ValueError("invalid base64url") from err


def hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, utf8(data), hashlib.sha256).digest()


def hmac_hex(key: bytes, data: str, chars: int) -> str:
    return hmac_sha256(key, data).hex()[:chars]


def sha256(data: str) -> bytes:
    return hashlib.sha256(utf8(data)).digest()


def _reject_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def as_number(v: Any) -> Optional[float]:
    if not is_number(v):
        return None
    try:
        return float(v)
    except OverflowError:
        return math.inf if v > 0 else -math.inf


def safe_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if -MAX_SAFE_INTEGER <= v <= MAX_SAFE_INTEGER else None
    if isinstance(v, float) and math.isfinite(v) and v.is_integer() and abs(v) <= MAX_SAFE_INTEGER:
        return int(v)
    return None


def js_equals(v: Any, n: int) -> bool:
    return is_number(v) and v == n


def js_round(v: Any) -> Optional[int]:
    if isinstance(v, (bool, str, bytes)):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal):
        return math.floor(v + Decimal("0.5")) if v.is_finite() else None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    r = math.floor(f)
    return r + 1 if f - r >= 0.5 else r


def decode_uri_component(s: str) -> str:
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i] != "%":
            j = s.find("%", i)
            j = n if j < 0 else j
            out.append(s[i:j])
            i = j
            continue
        buf = bytearray()
        while i < n and s[i] == "%":
            pair = s[i + 1 : i + 3]
            if len(pair) != 2 or pair[0] not in _HEX or pair[1] not in _HEX:
                raise ValueError("malformed URI escape")
            buf.append(int(pair, 16))
            i += 3
        out.append(buf.decode("utf-8"))
    return "".join(out)


def parse_header_int(raw: Optional[str]) -> Optional[int]:
    if not isinstance(raw, str):
        return None
    s = js_trim(raw)
    if _DIGITS_RE.fullmatch(s):
        v = int(s)
        return v if v <= MAX_SAFE_INTEGER else None
    if _DECIMAL_RE.fullmatch(s):
        f = safe_int(float(s))
        return f if f is not None and f >= 0 else None
    return None
