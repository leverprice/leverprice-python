from __future__ import annotations

import importlib.abc
import os
import sys
from typing import Any, Dict, Optional, Sequence

import pytest


class _BlockCryptography(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Optional[Sequence[str]], target: Any = None) -> None:
        if fullname == "cryptography" or fullname.startswith("cryptography."):
            raise ModuleNotFoundError(f"{fullname} is blocked by LEVERPRICE_TEST_NO_CRYPTOGRAPHY")
        return


if os.environ.get("LEVERPRICE_TEST_NO_CRYPTOGRAPHY") == "1":
    sys.meta_path.insert(0, _BlockCryptography())

from _support import load_vectors  # noqa: E402
from leverprice import _crypto  # noqa: E402


def pytest_report_header() -> str:
    return f"leverprice Ed25519 backend: {_crypto.backend()}"


@pytest.fixture(scope="session")
def vectors() -> Dict[str, Any]:
    return load_vectors()


@pytest.fixture(params=["native", "pure"])
def ed25519_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "native":
        if _crypto.backend() != "cryptography":
            pytest.skip("cryptography is not installed")
    else:
        monkeypatch.setattr(_crypto, "_native", None)
    param: str = request.param
    return param


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEVERPRICE_DISABLED", raising=False)
