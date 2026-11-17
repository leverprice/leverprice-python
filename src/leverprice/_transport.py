from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Union


@dataclass
class TransportResponse:
    """An HTTP response as returned by a transport."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Union[bytes, str] = b""


class Transport(Protocol):
    """Sends one HTTP request. A plain callable with the same signature also works."""

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> TransportResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class UrllibTransport:
    """The default transport (stdlib ``urllib``). Honours proxy environment variables; never redirects."""

    def __init__(self, opener: Optional[urllib.request.OpenerDirector] = None) -> None:
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> TransportResponse:
        req = urllib.request.Request(url, data=body, method=method)
        for name, value in headers.items():
            req.add_header(name, value)
        try:
            with self._opener.open(req, timeout=timeout) as res:
                return TransportResponse(res.status, dict(res.headers.items()), res.read())
        except urllib.error.HTTPError as err:
            try:
                data = err.read()
            except Exception:
                data = b""
            headers_out = dict(err.headers.items()) if err.headers is not None else {}
            err.close()
            return TransportResponse(err.code, headers_out, data)
