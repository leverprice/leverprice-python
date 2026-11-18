from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypeVar

from ._crypto import ed25519_verify
from ._util import LeverpriceError, as_number, from_b64url, from_utf8, js_equals, json_loads, safe_int, sha256
from ._wire import Config, ConfigError, ConfigErrorCode, parse_config

ROOT_KEY_PLACEHOLDER = "REPLACE_WITH_ROOT_PUBLIC_KEY"
_KEYSET_CACHE_SIZE = 8

_T = TypeVar("_T")


@dataclass(frozen=True)
class SigningKey:
    id: str
    public_key: str


@dataclass(frozen=True)
class Keyset:
    version: int
    issued_at: float
    expires_at: Optional[float]
    keys: Tuple[SigningKey, ...]


@dataclass(frozen=True)
class VerifiedConfig:
    config: Config
    signing_key: SigningKey
    dropped: List[str]


class TrustChain:
    def __init__(self, root_keys: Mapping[str, str], min_keyset_version: int) -> None:
        self._root_keys = dict(root_keys)
        self._min_keyset_version = min_keyset_version
        self._verified: Dict[str, Keyset] = {}
        self.newest: Optional[Keyset] = None

    def is_trusted(self, key: SigningKey) -> bool:
        newest = self.newest
        return newest is None or any(k.id == key.id and k.public_key == key.public_key for k in newest.keys)

    def verify(self, envelope: Any, now: float) -> VerifiedConfig:
        if not isinstance(envelope, dict):
            raise ConfigError("malformed", "Leverprice config: response is not a signed config")
        payload, signature, key_id = envelope.get("payload"), envelope.get("signature"), envelope.get("keyId")
        if not isinstance(payload, str) or not isinstance(signature, str) or not isinstance(key_id, str):
            raise ConfigError("malformed", "Leverprice config: response is not a signed config")
        keyset = _coded("untrusted", lambda: self._verify_keyset(envelope.get("keyset"), now))
        key = next((k for k in keyset.keys if k.id == key_id), None)
        if key is None:
            message = f'Leverprice config: signing key "{key_id}" is not in keyset v{keyset.version}'
            raise ConfigError("bad_signature", message)
        if not ed25519_verify(key.public_key, signature, payload):
            raise ConfigError("bad_signature", f'Leverprice config: bad signature (key "{key_id}")')
        config, dropped = _coded("malformed", lambda: parse_config(payload))
        if now >= config.expires_at:
            message = f"Leverprice config v{config.version}: expired at {_iso(config.expires_at)}"
            raise ConfigError("expired", message)
        return VerifiedConfig(config, key, dropped)

    def _verify_keyset(self, signed: Any, now: float) -> Keyset:
        if not isinstance(signed, dict) or not all(
            isinstance(signed.get(k), str) for k in ("payload", "signature", "rootKeyId")
        ):
            raise LeverpriceError("Leverprice keyset: missing or malformed")
        payload: str = signed["payload"]
        root_key_id: str = signed["rootKeyId"]
        keyset = self._verified.get(payload)
        if keyset is None:
            root = self._root_keys.get(root_key_id)
            if not isinstance(root, str) or not root:
                raise LeverpriceError(f'Leverprice keyset: root key "{root_key_id}" is not trusted by this SDK')
            if root == ROOT_KEY_PLACEHOLDER:
                raise LeverpriceError(
                    "Leverprice: this SDK build has no root public key (ROOT_PUBLIC_KEYS is a placeholder); "
                    "pass trusted_root_keys"
                )
            if not ed25519_verify(root, signed["signature"], payload):
                raise LeverpriceError(f'Leverprice keyset: signature does not match root key "{root_key_id}"')
            keyset = _parse_keyset(payload)
            if len(self._verified) >= _KEYSET_CACHE_SIZE:
                self._verified.clear()
            self._verified[payload] = keyset
        if keyset.expires_at is not None and now >= keyset.expires_at:
            raise LeverpriceError(f"Leverprice keyset v{keyset.version}: expired")
        if keyset.version < self._min_keyset_version:
            raise LeverpriceError(
                f"Leverprice keyset v{keyset.version}: below the minimum version {self._min_keyset_version}"
            )
        newest = self.newest
        if newest is not None and keyset.version < newest.version:
            raise LeverpriceError(
                f"Leverprice keyset v{keyset.version}: older than v{newest.version} already seen "
                "(revoked keys cannot come back)"
            )
        if newest is None or keyset.version > newest.version:
            self.newest = keyset
        return keyset


def _parse_keyset(payload: str) -> Keyset:
    try:
        raw = json_loads(from_utf8(from_b64url(payload)))
    except Exception:
        raise LeverpriceError("Leverprice keyset: payload is not base64url JSON") from None
    version = safe_int(raw.get("version")) if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or not js_equals(raw.get("schema"), 1)
        or version is None
        or not isinstance(raw.get("keys"), list)
    ):
        raise LeverpriceError("Leverprice keyset: unsupported schema")
    keys = tuple(
        SigningKey(k["id"], k["publicKey"])
        for k in raw["keys"]
        if isinstance(k, dict) and isinstance(k.get("id"), str) and isinstance(k.get("publicKey"), str)
    )
    return Keyset(
        version=version,
        issued_at=as_number(raw.get("issuedAt")) or 0.0,
        expires_at=as_number(raw.get("expiresAt")),
        keys=keys,
    )


def _iso(ms: float) -> str:
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return str(ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def key_fingerprint(secret_key: str) -> str:
    return sha256(secret_key).hex()[:16]


def accept_config(
    trust: TrustChain, fingerprint: str, current: Optional[Config], envelope: Any, now: float
) -> VerifiedConfig:
    verified = trust.verify(envelope, now)
    config = verified.config
    # A validly signed config of another project (e.g. from a compromised cache) is refused.
    if fingerprint not in config.key_fingerprints:
        raise ConfigError("wrong_key", "Leverprice config: not issued for this API key; refusing")
    if current is not None and config.project_id != current.project_id:
        raise ConfigError(
            "wrong_project",
            f"Leverprice config: project changed from {current.project_id} to {config.project_id}; refusing",
        )
    if current is not None and config.version < current.version:
        raise ConfigError(
            "rollback",
            f"Leverprice config: refusing to roll back from version {current.version} to {config.version}",
        )
    return verified


def _coded(code: ConfigErrorCode, run: Callable[[], _T]) -> _T:
    try:
        return run()
    except ConfigError:
        raise
    except Exception as err:
        raise ConfigError(code, str(err)) from err
