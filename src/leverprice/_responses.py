from __future__ import annotations

from http.cookies import BaseCookie
from typing import Any, Iterable, TypeVar, Union

from ._client import Quote

R = TypeVar("R")


def apply_cookies(response: R, quote: Union[Quote, Iterable[str]]) -> R:
    """Appends ``quote.set_cookie`` to a Django, Flask/Werkzeug, Starlette/FastAPI or aiohttp response.

    Also accepts a WSGI header list. Returns the response.
    """
    values = quote.set_cookie if isinstance(quote, Quote) else list(quote)
    for value in values:
        _append(response, value)
    return response


def _append(response: Any, value: str) -> None:
    if isinstance(response, list):
        response.append(("Set-Cookie", value))
        return
    cookies = getattr(response, "cookies", None)
    if isinstance(cookies, BaseCookie):
        cookies.load(value)
        return
    headers = getattr(response, "headers", response)
    for method, name in (("add", "Set-Cookie"), ("append", "set-cookie")):
        fn = getattr(headers, method, None)
        if callable(fn):
            fn(name, value)
            return
    raise TypeError(f"Leverprice: cannot set cookies on {type(response).__name__}; append quote.set_cookie yourself")
