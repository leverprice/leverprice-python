from __future__ import annotations

import contextlib
import re
import string
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

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
# Headers every browser or client sends itself: never a source for the visitor's country.
_VISITOR_SET_PREFIX_RE = re.compile(r"sec-|accept|content-|if-|x-forwarded-|proxy-")
_VISITOR_SET_HEADERS = frozenset(
    (
        "authorization",
        "cache-control",
        "connection",
        "cookie",
        "dnt",
        "forwarded",
        "host",
        "origin",
        "pragma",
        "priority",
        "purpose",
        "range",
        "referer",
        "te",
        "upgrade-insecure-requests",
        "user-agent",
        "via",
        "x-real-ip",
        "x-requested-with",
    )
)
# Headers proxies and hosts set to the visitor's IP address, in the order they are read.
_ADDRESS_HEADERS = (
    "cf-connecting-ip",
    "true-client-ip",
    "fastly-client-ip",
    "fly-client-ip",
    "x-real-ip",
    "x-forwarded-for",
)
_IPV4_RE = re.compile(r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})")
_HEX_GROUP_RE = re.compile(r"[0-9A-Fa-f]{1,4}")
_PORT_RE = re.compile(r":[0-9]{1,5}")
_IPV4_PORT_RE = re.compile(r"[0-9.]+:[0-9]{1,5}")


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


def visitor_set_header(name: str) -> bool:
    """A header the visitor's browser sends: naming it as the country source would let visitors pick a price."""
    lower = name.translate(_ASCII_LOWER)
    return _VISITOR_SET_PREFIX_RE.match(lower) is not None or lower in _VISITOR_SET_HEADERS


def client_address(source: Any, request: Any) -> Optional[str]:
    """The visitor's address: the first address header that holds one, else the connection's."""
    for name in _ADDRESS_HEADERS:
        value = read_header(source, name)
        if value is None:
            continue
        address = normalize_address(value.split(",")[0])
        if address is not None:
            return address
    for value in _connection_addresses(request):
        address = normalize_address(value)
        if address is not None:
            return address
    return None


def _connection_addresses(request: Any) -> List[Any]:
    """Where frameworks keep the peer's address: a WSGI environ, an ASGI scope or the request object."""
    if isinstance(request, dict):
        if request.get("type") in ("http", "websocket"):
            return [_host(request.get("client"))]
        return [request.get("REMOTE_ADDR")] if is_wsgi_environ(request) else []
    if request is None or isinstance(request, _SCALARS):
        return []
    # Flask/Werkzeug (remote_addr), aiohttp (remote), Starlette/FastAPI (client.host), Django (META).
    readers: Tuple[Callable[[], Any], ...] = (
        lambda: request.remote_addr,
        lambda: request.remote,
        lambda: request.client.host,
        lambda: request.META["REMOTE_ADDR"],
        lambda: request.environ["REMOTE_ADDR"],
        lambda: _host(request.scope["client"]),
    )
    found: List[Any] = []
    for read in readers:
        try:
            found.append(read())
        except Exception:
            continue
    return found


def _host(client: Any) -> Any:
    return client[0] if isinstance(client, (list, tuple)) and client else None


def _ipv4(text: str) -> Optional[List[int]]:
    match = _IPV4_RE.fullmatch(text)
    if match is None:
        return None
    parts = [int(group) for group in match.groups()]
    return parts if all(n <= 255 for n in parts) else None


def _ipv6(text: str) -> Optional[List[int]]:
    s = text
    if "." in s:
        colon = s.rfind(":")
        v4 = _ipv4(s[colon + 1 :]) if colon >= 0 else None
        if v4 is None:
            return None
        s = f"{s[: colon + 1]}{v4[0] << 8 | v4[1]:x}:{v4[2] << 8 | v4[3]:x}"
    halves = s.split("::")
    if len(halves) > 2:
        return None
    head = halves[0].split(":") if halves[0] else []
    tail = (halves[1].split(":") if halves[1] else []) if len(halves) == 2 else None
    groups = head + (tail or [])
    if not all(_HEX_GROUP_RE.fullmatch(group) for group in groups):
        return None
    if tail is None:
        return [int(group, 16) for group in groups] if len(groups) == 8 else None
    if len(groups) > 7:
        return None
    return [int(group, 16) for group in head] + [0] * (8 - len(groups)) + [int(group, 16) for group in tail]


def normalize_address(value: Any) -> Optional[str]:
    """IPv4 in decimal, IPv6 cut to its /64 network as four lowercase hex groups; None for anything else.

    Written by hand: ``ipaddress`` accepts and refuses different spellings than the other SDKs.
    """
    if not isinstance(value, str):
        return None
    s = value.strip(" \t")
    if s.startswith("["):
        end = s.find("]")
        if end < 0 or (end + 1 < len(s) and not _PORT_RE.fullmatch(s[end + 1 :])):
            return None
        s = s[1:end]
    elif _IPV4_PORT_RE.fullmatch(s):
        s = s[: s.index(":")]
    zone = s.find("%")
    if zone >= 0:
        s = s[:zone]
    v4 = _ipv4(s)
    if v4 is not None:
        return ".".join(str(n) for n in v4)
    v6 = _ipv6(s)
    if v6 is None:
        return None
    if not any(v6[:5]) and v6[5] == 0xFFFF:
        return ".".join(str(n) for n in (v6[6] >> 8, v6[6] & 255, v6[7] >> 8, v6[7] & 255))
    return ":".join(f"{group:x}" for group in v6[:4])


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
