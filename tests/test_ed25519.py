from __future__ import annotations

import hashlib
import os
from typing import Tuple

import pytest

from _support import b64url, public_key, sign
from leverprice import _crypto, _ed25519

RFC8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


def _vector(case: Tuple[str, str, str, str]) -> Tuple[bytes, bytes, bytes, bytes]:
    seed, pub, msg, sig = (bytes.fromhex(x) for x in case)
    return seed, pub, msg, sig


@pytest.mark.parametrize("case", RFC8032, ids=["test1", "test2"])
def test_rfc8032_vectors(case: Tuple[str, str, str, str], ed25519_backend: str) -> None:
    seed, pub, msg, sig = _vector(case)
    assert public_key(seed) == pub
    assert sign(seed, msg) == sig
    assert _crypto.verify_raw(pub, sig, msg)
    assert not _crypto.verify_raw(pub, sig, msg + b"x")


def test_signer_round_trip(ed25519_backend: str) -> None:
    for i in range(16):
        seed = hashlib.sha256(bytes([i])).digest()
        message = os.urandom(i * 7)
        signature = sign(seed, message)
        assert _crypto.verify_raw(public_key(seed), signature, message)
        tampered = bytearray(signature)
        tampered[i % 64] ^= 0x40
        assert not _crypto.verify_raw(public_key(seed), bytes(tampered), message)


def test_non_canonical_encodings(ed25519_backend: str) -> None:
    _seed, pub, msg, sig = _vector(RFC8032[0])
    s = int.from_bytes(sig[32:], "little")
    assert not _crypto.verify_raw(pub, sig[:32] + (s + _ed25519.L).to_bytes(32, "little"), msg)
    assert not _crypto.verify_raw(pub, (_ed25519.P + 1).to_bytes(32, "little") + sig[32:], msg)
    non_canonical_key = (_ed25519.P + 1).to_bytes(32, "little")
    assert _ed25519.decode_point(non_canonical_key) is None
    assert not _crypto.verify_raw(non_canonical_key, sig, msg)
    assert _ed25519.decode_point((1 | (1 << 255)).to_bytes(32, "little")) is None


def test_malformed_inputs_are_not_verified(ed25519_backend: str) -> None:
    _, pub, msg, sig = _vector(RFC8032[0])
    assert _crypto.ed25519_verify(b64url(pub), b64url(sig), msg.decode())
    assert not _crypto.ed25519_verify(b64url(pub) + "=", b64url(sig), "")
    assert not _crypto.ed25519_verify(b64url(pub[:31]), b64url(sig), "")
    assert not _crypto.ed25519_verify(b64url(pub), b64url(sig[:63]), "")
    assert not _crypto.ed25519_verify(b64url(pub), "not base64!", "")
    assert not _crypto.ed25519_verify(b64url(pub), b64url(sig), "\ud800")


def test_pure_and_native_agree() -> None:
    if _crypto.backend() != "cryptography":
        pytest.skip("cryptography is not installed")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    for i in range(20):
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        message = os.urandom(i)
        signature = key.sign(message)
        assert _ed25519.verify(pub, signature, message)
        assert not _ed25519.verify(pub, signature, message + b"!")


def test_backend_reported() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        expected = "pure"
    else:
        expected = "cryptography"
    assert _crypto.backend() == expected
