from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._util import COUNTRY_RE, ID_RE, b64url, from_b64url, from_utf8, hmac_sha256, json_loads, safe_int, utf8

MAX_COOKIE_EXPERIMENTS = 10
MAX_COOKIE_LENGTH = 4096
PIN_MAX_AGE_SECONDS = 1800

_VISITOR_KEY_RE = re.compile(r"[0-9a-f]{32}")
_BODY_RE = re.compile(r"[A-Za-z0-9_-]+")
_SIG_RE = re.compile(r"[A-Za-z0-9_-]{22}")


@dataclass
class AssignmentState:
    v: str
    a: Dict[str, str] = field(default_factory=dict)
    x: List[str] = field(default_factory=list)
    c: Optional[str] = None
    """The country whose price the visitor holds (VPN guard)."""
    t: Optional[int] = None
    """When ``c`` was set, in seconds since the epoch."""

    def clone(self) -> AssignmentState:
        return AssignmentState(self.v, dict(self.a), list(self.x), self.c, self.t)


def assign(state: AssignmentState, experiment_id: str, variant_id: str) -> None:
    pairs = [(experiment_id, variant_id)] + [(k, v) for k, v in state.a.items() if k != experiment_id]
    state.a = dict(pairs[:MAX_COOKIE_EXPERIMENTS])
    state.x = [i for i in state.x if i in state.a]


def mark_exposed(state: AssignmentState, experiment_id: str) -> bool:
    if experiment_id in state.x:
        return False
    state.x = [i for i in [experiment_id, *state.x] if i in state.a][:MAX_COOKIE_EXPERIMENTS]
    return True


def state_json(state: AssignmentState) -> str:
    data: Dict[str, Any] = {"v": state.v, "a": state.a, "x": state.x}
    if state.c is not None and state.t is not None:
        data["c"], data["t"] = state.c, state.t
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def sign_assignment(key: bytes, state: AssignmentState) -> str:
    body = b64url(utf8(state_json(state)))
    sig = b64url(hmac_sha256(key, "pp:cookie:v1:" + body)[:16])
    return f"{body}.{sig}"


def verify_assignment(key: bytes, raw: str) -> Optional[AssignmentState]:
    if not isinstance(raw, str) or len(raw) > MAX_COOKIE_LENGTH:
        return None
    dot = raw.find(".")
    if dot <= 0:
        return None
    body, sig = raw[:dot], raw[dot + 1 :]
    if not _BODY_RE.fullmatch(body) or not _SIG_RE.fullmatch(sig):
        return None
    try:
        expected = hmac_sha256(key, "pp:cookie:v1:" + body)[:16]
        if not hmac.compare_digest(expected, from_b64url(sig)):
            return None
        return _normalize_state(json_loads(from_utf8(from_b64url(body))))
    except Exception:
        return None


def _normalize_state(raw: Any) -> Optional[AssignmentState]:
    if not isinstance(raw, dict):
        return None
    v, raw_a = raw.get("v"), raw.get("a")
    if not isinstance(v, str) or not _VISITOR_KEY_RE.fullmatch(v) or not isinstance(raw_a, dict):
        return None
    a = {
        exp: variant
        for exp, variant in list(raw_a.items())[:MAX_COOKIE_EXPERIMENTS]
        if ID_RE.fullmatch(exp) and isinstance(variant, str) and ID_RE.fullmatch(variant)
    }
    raw_x = raw.get("x")
    x = [i for i in raw_x if isinstance(i, str) and i in a][:MAX_COOKIE_EXPERIMENTS] if isinstance(raw_x, list) else []
    c, t = raw.get("c"), safe_int(raw.get("t"))
    if isinstance(c, str) and COUNTRY_RE.fullmatch(c) and t is not None and t >= 0:
        return AssignmentState(v, a, x, c, t)
    return AssignmentState(v, a, x)


def set_cookie_header(name: str, value: str, max_age: int, secure: bool, domain: Optional[str]) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "SameSite=Lax", "HttpOnly"]
    if secure:
        parts.append("Secure")
    if domain:
        parts.append(f"Domain={domain}")
    return "; ".join(parts)
