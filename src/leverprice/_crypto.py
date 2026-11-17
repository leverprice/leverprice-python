from __future__ import annotations

from typing import Callable, Optional

from . import _ed25519
from ._util import from_b64url, utf8

_Verifier = Callable[[bytes, bytes, bytes], bool]


class _UnsupportedError(Exception):
    pass


def _load_native() -> Optional[_Verifier]:
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return None

    def verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
            return True
        except InvalidSignature:
            return False
        except UnsupportedAlgorithm as err:
            raise _UnsupportedError from err
        except Exception:
            return False

    return verify


_native: Optional[_Verifier] = _load_native()


def backend() -> str:
    return "cryptography" if _native is not None else "pure"


def verify_raw(public_key: bytes, signature: bytes, message: bytes) -> bool:
    global _native
    if len(public_key) != 32 or len(signature) != 64:
        return False
    if _ed25519.decode_point(public_key) is None:
        return False
    native = _native
    if native is not None:
        try:
            return native(public_key, signature, message)
        except _UnsupportedError:
            _native = None
    return _ed25519.verify(public_key, signature, message)


def ed25519_verify(public_key: str, signature: str, data: str) -> bool:
    try:
        return verify_raw(from_b64url(public_key), from_b64url(signature), utf8(data))
    except Exception:
        return False
