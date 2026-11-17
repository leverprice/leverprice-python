from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

Point = Tuple[int, int, int, int]

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, P - 2, P) % P
_D2 = 2 * _D % P
_SQRT_M1 = pow(2, (P - 1) // 4, P)
IDENTITY: Point = (0, 1, 1, 0)


def _add(p: Point, q: Point) -> Point:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * _D2 * t2 % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _double(p: Point) -> Point:
    x1, y1, z1, _ = p
    a = x1 * x1 % P
    b = y1 * y1 % P
    c = 2 * z1 * z1 % P
    h = a + b
    e = (h - (x1 + y1) * (x1 + y1)) % P
    g = a - b
    f = c + g
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def decode_point(b: bytes) -> Optional[Point]:
    if len(b) != 32:
        return None
    n = int.from_bytes(b, "little")
    y = n & ((1 << 255) - 1)
    x = _recover_x(y, n >> 255)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def encode_point(p: Point) -> bytes:
    x, y, z, _ = p
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _table(p: Point) -> List[Point]:
    t = [IDENTITY, p]
    for _ in range(14):
        t.append(_add(t[-1], p))
    return t


_BY = 4 * pow(5, P - 2, P) % P
_BX = _recover_x(_BY, 0) or 0
BASE: Point = (_BX, _BY, 1, _BX * _BY % P)
_BASE_TABLE = _table(BASE)


def double_scalarmult(s: int, k: int, q: Point) -> Point:
    tq = _table(q) if k else None
    r = IDENTITY
    for i in range(63, -1, -1):
        r = _double(_double(_double(_double(r))))
        ns = (s >> (4 * i)) & 15
        if ns:
            r = _add(r, _BASE_TABLE[ns])
        if tq is not None:
            nk = (k >> (4 * i)) & 15
            if nk:
                r = _add(r, tq[nk])
    return r


def verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = decode_point(public_key)
    if a is None:
        return False
    r_bytes = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(r_bytes + public_key + message).digest(), "little") % L
    neg_a: Point = (-a[0] % P, a[1], a[2], -a[3] % P)
    # Comparing encodings rejects a non-canonical R as well as a wrong one.
    return encode_point(double_scalarmult(s, k, neg_a)) == r_bytes
