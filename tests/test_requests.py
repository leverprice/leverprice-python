from __future__ import annotations

import email.message
from http.cookies import SimpleCookie
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import pytest

from _support import CHROME, GOOGLEBOT, FakeServer, Headers, SigningKeys, config_payload, make_client
from leverprice import Leverprice, Quote, apply_cookies
from leverprice._request import header_source, read_header, read_request

KEYS = SigningKeys()
COOKIE = "__pp=abc.def; theme=dark"


@pytest.fixture
def lp() -> Leverprice:
    return make_client(FakeServer(KEYS.envelope(config_payload())))


class CaseInsensitive(Mapping[str, str]):
    def __init__(self, data: Dict[str, str]) -> None:
        self._data = {k.lower(): v for k, v in data.items()}

    def __getitem__(self, key: str) -> str:
        return self._data[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class MultiHeaders:
    def __init__(self, pairs: List[Tuple[str, str]], method: str) -> None:
        self._pairs = pairs
        setattr(self, method, self._all)

    def _all(self, name: str) -> List[str]:
        values = [v for k, v in self._pairs if k.lower() == name.lower()]
        if not values and isinstance(self, StrictMulti):
            raise KeyError(name)
        return values


class StrictMulti(MultiHeaders):
    pass


class DjangoLike:
    def __init__(self, meta: Dict[str, str]) -> None:
        self.META = meta


class Proxy:
    def __init__(self, target: Any) -> None:
        self._target = target

    def _get_current_object(self) -> Any:
        return self._target


def info(request: Any) -> Tuple[Optional[str], Optional[str], bool]:
    req = read_request(header_source(request), None, None, "__pp")
    return req.cookie_input, req.user_agent, req.prefetch


EXPECTED = (COOKIE, CHROME, True)


@pytest.mark.parametrize(
    "request_obj",
    [
        {"Cookie": COOKIE, "User-Agent": CHROME, "Sec-Purpose": "prefetch"},
        {"COOKIE": [COOKIE], "user-agent": CHROME, "x-moz": "prefetch"},
        {"HTTP_COOKIE": COOKIE, "HTTP_USER_AGENT": CHROME, "HTTP_PURPOSE": "prefetch", "REQUEST_METHOD": "GET"},
        Headers({"cookie": COOKIE, "user-agent": CHROME, "x-purpose": "preview"}),
        Headers(CaseInsensitive({"Cookie": COOKIE, "User-Agent": CHROME, "Next-Router-Prefetch": "1"})),
        DjangoLike({"HTTP_COOKIE": COOKIE, "HTTP_USER_AGENT": CHROME, "HTTP_SEC_PURPOSE": "prefetch"}),
        Proxy(Headers({"Cookie": COOKIE, "User-Agent": CHROME, "Purpose": "prefetch"})),
        [("cookie", COOKIE), ("user-agent", CHROME), ("sec-purpose", "prefetch")],
        {
            "type": "http",
            "headers": [(b"cookie", COOKIE.encode()), (b"user-agent", CHROME.encode()), (b"sec-purpose", b"prefetch")],
        },
    ],
    ids=["dict", "dict-list", "environ", "headers", "ci-mapping", "meta", "proxy", "pairs", "asgi-scope"],
)
def test_request_shapes(request_obj: Any) -> None:
    from leverprice._request import unwrap

    assert info(unwrap(request_obj)) == EXPECTED


@pytest.mark.parametrize("method", ["getlist", "getall", "get_list"])
def test_multi_value_headers(method: str) -> None:
    pairs = [("Cookie", "__pp=one"), ("cookie", "theme=dark"), ("User-Agent", CHROME), ("Accept", "a"), ("accept", "b")]
    headers = MultiHeaders(pairs, method)
    assert read_header(headers, "cookie") == "__pp=one; theme=dark"
    assert read_header(headers, "accept") == "a, b"
    assert read_header(headers, "sec-purpose") is None
    strict = StrictMulti(pairs, method)
    assert read_header(strict, "x-moz") is None


def test_email_message_headers() -> None:
    message = email.message.Message()
    message["Cookie"] = "__pp=one"
    message["User-Agent"] = CHROME
    assert info(Headers(message)) == ("__pp=one", CHROME, False)  # type: ignore[arg-type]


def test_missing_and_odd_requests() -> None:
    for request in (None, "cookie", 42, object(), Headers(None)):  # type: ignore[arg-type]
        assert info(request) == (None, None, False)


def test_explicit_values_win(lp: Leverprice) -> None:
    request = {"cookie": "__pp_f=1", "user-agent": GOOGLEBOT}
    assert lp.get_price("pro", request).reason == "bot"
    quote = lp.get_price("pro", request, cookie="theme=dark", user_agent=CHROME)
    assert quote.reason == "experiment"


def test_django_request(lp: Leverprice) -> None:
    django = pytest.importorskip("django")
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEFAULT_CHARSET="utf-8", ALLOWED_HOSTS=["*"])
    django.setup()
    from django.http import HttpResponse
    from django.test import RequestFactory

    request = RequestFactory().get("/", HTTP_USER_AGENT=CHROME, HTTP_COOKIE="theme=dark", HTTP_SEC_PURPOSE="prefetch")
    pro = lp.get_price("pro", request)
    team = lp.get_price("team", request)
    assert pro.reason == "experiment"
    assert not pro.exposure_logged
    assert pro.visitor_id == team.visitor_id
    response = apply_cookies(HttpResponse("ok"), team)
    header = response.cookies["__pp"].output(header="Set-Cookie:")
    for part in ("Max-Age=15552000", "Path=/", "SameSite=Lax", "HttpOnly", "Secure"):
        assert part in header


def test_flask_request(lp: Leverprice) -> None:
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"User-Agent": CHROME, "Cookie": "theme=dark"}):
        pro = lp.get_price("pro", flask.request)
        team = lp.get_price("team", flask.request)
        assert pro.visitor_id == team.visitor_id
        response = apply_cookies(flask.make_response("ok"), team)
        assert response.headers.getlist("Set-Cookie") == team.set_cookie
    with app.test_request_context("/", headers={"User-Agent": CHROME}):
        assert lp.get_price("pro", flask.request).visitor_id != pro.visitor_id


def test_starlette_request(lp: Leverprice) -> None:
    pytest.importorskip("starlette")
    from starlette.requests import Request
    from starlette.responses import Response

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"user-agent", CHROME.encode()), (b"cookie", b"a=1"), (b"cookie", b"__pp_f=1")],
    }
    request = Request(scope)
    assert lp.get_price("pro", request).reason == "pinned_fallback"
    fresh = Request({**scope, "headers": scope["headers"][:1]})
    quote = lp.get_price("pro", fresh)
    response = apply_cookies(Response("ok"), quote)
    assert response.headers.getlist("set-cookie") == quote.set_cookie


def test_multidict_headers(lp: Leverprice) -> None:
    multidict = pytest.importorskip("multidict")
    headers = multidict.CIMultiDict([("User-Agent", CHROME), ("Cookie", "a=1"), ("Cookie", "__pp_f=1")])
    assert lp.get_price("pro", Headers(headers)).reason == "pinned_fallback"  # type: ignore[arg-type]

    class AiohttpResponse:
        def __init__(self) -> None:
            self.headers = multidict.CIMultiDict()

    quote = Quote("pro", 1, "usd", True, "no_config", set_cookie=["a=1", "b=2"])
    assert apply_cookies(AiohttpResponse(), quote).headers.getall("Set-Cookie") == ["a=1", "b=2"]


def test_apply_cookies_shapes() -> None:
    quote = Quote("pro", 1, "usd", True, "no_config", set_cookie=["__pp_f=1; Path=/; Max-Age=1800; HttpOnly"])
    wsgi: List[Tuple[str, str]] = []
    assert apply_cookies(wsgi, quote) == [("Set-Cookie", quote.set_cookie[0])]

    class CookieJarResponse:
        def __init__(self) -> None:
            self.cookies: SimpleCookie = SimpleCookie()

    jar = apply_cookies(CookieJarResponse(), ["a=b; Path=/; Max-Age=60; HttpOnly"]).cookies
    assert jar["a"].value == "b"
    assert jar["a"]["max-age"] == "60"
    assert jar["a"]["httponly"]
    with pytest.raises(TypeError, match="cannot set cookies"):
        apply_cookies(object(), quote)
