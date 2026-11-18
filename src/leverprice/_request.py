from __future__ import annotations

import contextlib
import re
import string
from dataclasses import dataclass
from typing import Any, List, Optional

from ._util import ASCII_WHITESPACE, decode_uri_component, js_trim

_PURPOSE_RE = re.compile(r"prefetch|prerender|preview", re.IGNORECASE | re.ASCII)
_SPLIT_RE = re.compile(r"[;,]")
_MULTI_GETTERS = ("getlist", "getall", "get_all", "get_list")
_SCALARS = (dict, list, tuple, str, bytes)
# Two ASCII letters with ASCII whitespace around them at most: case mapping must never invent a code.
_ASCII_SPACES = "[" + re.escape(ASCII_WHITESPACE) + "]*"
_COUNTRY_INPUT_RE = re.compile(_ASCII_SPACES + "([A-Za-z]{2})" + _ASCII_SPACES)
# Header names are ASCII: Unicode case mapping would let U+212A stand in for "k".
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_ASCII_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
# What CDNs send for an unknown country, Tor, anonymous proxies and non-countries.
_NOT_A_COUNTRY = frozenset(("XX", "ZZ", "T1", "A1", "A2", "AP", "EU"))


@dataclass(frozen=True)
class RequestInfo:
    cookie_input: Optional[str]
    assignment_cookies: List[str]
    pinned: bool
    user_agent: Optional[str]
    prefetch: bool
    country: Optional[str]


def unwrap(request: Any) -> Any:
    if request is None or isinstance(request, _SCALARS):
        return request
    try:
        get = getattr(request, "_get_current_object", None)
        return get() if callable(get) else request
    except Exception:
        return None


def is_wsgi_environ(request: Any) -> bool:
    return isinstance(request, dict) and ("wsgi.version" in request or "REQUEST_METHOD" in request)


def header_source(request: Any) -> Any:
    if request is None or isinstance(request, (str, bytes)):
        return None
    if isinstance(request, dict):
        headers = request.get("headers")
        if request.get("type") in ("http", "websocket") and isinstance(headers, (list, tuple)):
            return list(headers)
        return request
    if isinstance(request, (list, tuple)):
        return request
    for attr in ("headers", "META", "environ"):
        try:
            value = getattr(request, attr, None)
        except Exception:
            value = None
        if value is not None and not callable(value) and not isinstance(value, (str, bytes)):
            return value
    if any(callable(getattr(request, a, None)) for a in ("get", *_MULTI_GETTERS)):
        return request
    return None


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("latin-1")]
    if isinstance(value, (list, tuple)):
        return [v if isinstance(v, str) else v.decode("latin-1") for v in value if isinstance(v, (str, bytes))]
    return []


def _name(key: Any) -> Optional[str]:
    if isinstance(key, bytes):
        return key.decode("latin-1")
    return key if isinstance(key, str) else None


def header_values(source: Any, name: str) -> List[str]:
    if source is None:
        return []
    lower = name.lower()
    cgi = "HTTP_" + name.upper().replace("-", "_")
    if isinstance(source, (list, tuple)):
        values: List[str] = []
        for pair in source:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                key = _name(pair[0])
                if key is not None and key.translate(_ASCII_LOWER) == lower:
                    values.extend(_strings(pair[1]))
        return values
    if not isinstance(source, dict):
        for attr in _MULTI_GETTERS:
            multi = getattr(source, attr, None)
            if callable(multi):
                try:
                    return _strings(multi(lower))
                except Exception:
                    return []
    getter = getattr(source, "get", None)
    if callable(getter):
        for key in (lower, cgi):
            try:
                found = _strings(getter(key))
            except Exception:
                found = []
            if found:
                return found
    keys = getattr(source, "keys", None)
    if callable(keys):
        try:
            for key in list(keys()):
                k = _name(key)
                if k is not None and (k.translate(_ASCII_LOWER) == lower or k.translate(_ASCII_UPPER) == cgi):
                    found = _strings(source[key])
                    if found:
                        return found
        except Exception:
            return []
    return []


def read_header(source: Any, name: str) -> Optional[str]:
    values = header_values(source, name)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return ("; " if name.lower() == "cookie" else ", ").join(values)


def cookie_values(header: Optional[str], name: str) -> List[str]:
    if not header:
        return []
    values: List[str] = []
    # Split on "," as well: Fetch-style header joining merges repeated Cookie headers with ", ".
    for part in _SPLIT_RE.split(header):
        eq = part.find("=")
        if eq <= 0 or js_trim(part[:eq]) != name:
            continue
        value = js_trim(part[eq + 1 :])
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if "%" in value:
            with contextlib.suppress(ValueError):
                value = decode_uri_component(value)
        if value:
            values.append(value)
    return values


def is_prefetch(source: Any) -> bool:
    if source is None:
        return False
    for name in ("sec-purpose", "x-sec-purpose", "purpose", "x-purpose", "x-moz"):
        value = read_header(source, name)
        if value and _PURPOSE_RE.search(value):
            return True
    return read_header(source, "next-router-prefetch") == "1"


def normalize_country(value: Any) -> Optional[str]:
    match = _COUNTRY_INPUT_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    code: str = match.group(1).upper()
    return None if code in _NOT_A_COUNTRY else code


def read_request(
    source: Any,
    cookie: Any,
    user_agent: Any,
    cookie_name: str,
    country: Any = None,
    country_header: Optional[str] = None,
) -> RequestInfo:
    explicit = cookie if isinstance(cookie, str) else None
    cookie_input = explicit if explicit is not None else read_header(source, "cookie")
    pinned = False
    if explicit is not None and "=" not in explicit:
        bare = js_trim(explicit)
        assignment = [bare] if bare else []
    else:
        assignment = cookie_values(cookie_input, cookie_name)
        pinned = bool(cookie_values(cookie_input, cookie_name + "_f"))
    ua = user_agent if isinstance(user_agent, str) else read_header(source, "user-agent")
    if not isinstance(country, str):
        country = read_header(source, country_header) if country_header else None
    return RequestInfo(cookie_input, assignment, pinned, ua, is_prefetch(source), normalize_country(country))
