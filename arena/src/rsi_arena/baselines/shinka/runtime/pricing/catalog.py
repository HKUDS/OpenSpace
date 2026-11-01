"""Provider-qualified runtime pricing catalog and refresh lifecycle."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import httpx

ModelKind = Literal["llm", "embedding"]
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_USER_AGENT = "ShinkaEvolve pricing refresh"
MAX_CATALOG_BYTES = 10 * 1024 * 1024
MAX_CACHE_BYTES = 2 * MAX_CATALOG_BYTES
FETCH_DEADLINE_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 1.0
_CACHE_FILENAME = "models-dev-cache.json"

logger = logging.getLogger(__name__)


class PricingMode(str, Enum):
    AUTO = "auto"
    OFFLINE = "offline"
    REQUIRED = "required"


@dataclass(frozen=True)
class PricingConfig:
    mode: PricingMode | str = PricingMode.AUTO
    url: str = MODELS_DEV_URL
    cache_dir: Path | None = None


@dataclass(frozen=True)
class ModelPrice:
    model_name: str
    api_model_name: str
    provider: str
    kind: ModelKind
    input_price: float | None
    output_price: float | None = None
    input_price_tier2: float | None = None
    output_price_tier2: float | None = None
    tier_threshold: int | None = None
    is_reasoning: bool = False
    think_temp_fixed: bool = False
    requires_reasoning: bool = False

    def rates(
        self, input_tokens: int | None = None
    ) -> tuple[float | None, float | None]:
        use_tier2 = (
            input_tokens is not None
            and self.tier_threshold is not None
            and input_tokens > self.tier_threshold
        )
        if not use_tier2:
            return self.input_price, self.output_price
        return (
            self.input_price_tier2
            if self.input_price_tier2 is not None
            else self.input_price,
            self.output_price_tier2
            if self.output_price_tier2 is not None
            else self.output_price,
        )


class PricingCatalog:
    """Immutable model prices indexed by provider and API model identifier."""

    def __init__(self, entries: tuple[ModelPrice, ...] = ()) -> None:
        self._entries = entries
        self._by_key = {
            (entry.kind, entry.provider, entry.api_model_name): entry
            for entry in entries
        }
        self._by_name = {(entry.kind, entry.model_name): entry for entry in entries}

    @property
    def entries(self) -> tuple[ModelPrice, ...]:
        return self._entries

    def get(
        self,
        provider: str,
        model_name: str,
        *,
        kind: ModelKind = "llm",
    ) -> ModelPrice:
        try:
            return self._by_key[(kind, provider, model_name)]
        except KeyError as exc:
            raise KeyError(f"Unknown {kind} model: {provider}/{model_name}") from exc

    def get_by_name(self, model_name: str, *, kind: ModelKind = "llm") -> ModelPrice:
        try:
            return self._by_name[(kind, model_name)]
        except KeyError as exc:
            raise KeyError(f"Unknown {kind} model: {model_name}") from exc

    def find_by_api_name(
        self, model_name: str, *, kind: ModelKind = "llm"
    ) -> ModelPrice:
        matches = [
            entry
            for entry in self._entries
            if entry.kind == kind and entry.api_model_name == model_name
        ]
        if len(matches) != 1:
            raise KeyError(f"Ambiguous or unknown {kind} API model: {model_name}")
        return matches[0]

    def models(self, provider: str, *, kind: ModelKind) -> list[str]:
        return [
            entry.model_name
            for entry in self._entries
            if entry.kind == kind and entry.provider == provider
        ]

    def providers(self, *, kind: ModelKind) -> list[str]:
        return list(
            dict.fromkeys(
                entry.provider for entry in self._entries if entry.kind == kind
            )
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    catalog: PricingCatalog
    source: Literal["live", "cache", "bundled"]
    fetched_at: str | None
    etag: str | None
    sha256: str
    stale: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "etag": self.etag,
            "sha256": self.sha256,
            "stale": self.stale,
        }


_catalog_lock = threading.Lock()
_refresh_lock = threading.Lock()
_active_snapshot: CatalogSnapshot | None = None
_context_snapshot: ContextVar[CatalogSnapshot | None] = ContextVar(
    "shinka_pricing_snapshot", default=None
)


def get_catalog() -> CatalogSnapshot:
    global _active_snapshot
    context_snapshot = _context_snapshot.get()
    if context_snapshot is not None:
        return context_snapshot
    with _catalog_lock:
        if _active_snapshot is None:
            _active_snapshot = _bundled_snapshot(stale=False)
        return _active_snapshot


def catalog_from_models_dev_payload(
    payload: Any,
    overlay_path: Path | None = None,
    *,
    include_bundled: bool = True,
) -> PricingCatalog:
    """Normalize a models.dev payload with Shinka aliases and bundled fallback."""
    from .normalization import catalog_from_payload

    return catalog_from_payload(
        payload, overlay_path, include_bundled=include_bundled
    )


def refresh_model_catalog(
    config: PricingConfig | None = None,
    client: httpx.Client | None = None,
) -> CatalogSnapshot:
    config = config or _config_from_environment()
    with _refresh_lock:
        mode = PricingMode(config.mode)
        cache_path = _cache_path(config.cache_dir)
        cached = _validated_cache(_read_cache(cache_path))
        if mode == PricingMode.OFFLINE:
            snapshot = (
                _snapshot_from_cache(cached, stale=False)
                if cached
                else _bundled_snapshot(False)
            )
            return _activate(snapshot)
        return _refresh_online(config, client, cached, cache_path)


def write_run_pricing_snapshot(snapshot: CatalogSnapshot, results_dir: Path) -> Path:
    output_path = Path(results_dir) / "pricing_snapshot.json"
    if output_path.exists() and _parse_run_snapshot(output_path) is not None:
        return output_path
    document = {
        **snapshot.metadata(),
        "catalog": [asdict(entry) for entry in snapshot.catalog.entries],
        "catalog_sha256": _catalog_digest(snapshot.catalog.entries),
    }
    _write_json_atomic(output_path, document)
    return output_path


def load_run_pricing_snapshot(results_dir: Path) -> CatalogSnapshot | None:
    snapshot_path = Path(results_dir) / "pricing_snapshot.json"
    snapshot = _parse_run_snapshot(snapshot_path)
    if snapshot is None:
        if snapshot_path.exists():
            logger.warning("Ignoring invalid pricing snapshot: %s", snapshot_path)
        return None
    return _activate(snapshot)


def _parse_run_snapshot(snapshot_path: Path) -> CatalogSnapshot | None:
    try:
        if snapshot_path.stat().st_size > MAX_CACHE_BYTES:
            raise ValueError("pricing snapshot exceeds the safety limit")
        document = _loads_json(snapshot_path.read_text(encoding="utf-8"))
        entries = tuple(ModelPrice(**entry) for entry in document["catalog"])
        _validate_entries(entries)
        if document.get("catalog_sha256") != _catalog_digest(entries):
            raise ValueError("pricing snapshot catalog digest mismatch")
        source = document["source"]
        if source not in {"live", "cache", "bundled"}:
            raise ValueError("invalid pricing snapshot source")
        snapshot = CatalogSnapshot(
            catalog=PricingCatalog(entries),
            source=source,
            fetched_at=_optional_string(document.get("fetched_at")),
            etag=_optional_string(document.get("etag")),
            sha256=str(document["sha256"]),
            stale=bool(document.get("stale", False)),
        )
    except (
        FileNotFoundError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None
    return snapshot


def _refresh_online(
    config: PricingConfig,
    client: httpx.Client | None,
    cached: dict[str, Any] | None,
    cache_path: Path,
) -> CatalogSnapshot:
    try:
        response = _request_catalog(config.url, cached, client)
        snapshot = _snapshot_from_http(response, cached, cache_path)
        return _activate(snapshot)
    except (
        httpx.HTTPError,
        OSError,
        OverflowError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        if PricingMode(config.mode) == PricingMode.REQUIRED:
            raise RuntimeError("Unable to refresh required models.dev pricing") from exc
        logger.warning("models.dev pricing refresh failed; using fallback: %s", exc)
        fallback = (
            _snapshot_from_cache(cached, stale=True)
            if cached is not None
            else _bundled_snapshot(stale=True)
        )
        return _activate(fallback)


def _snapshot_from_http(
    response: httpx.Response,
    cached: dict[str, Any] | None,
    cache_path: Path,
) -> CatalogSnapshot:
    if response.status_code == 304:
        if cached is None:
            raise ValueError("models.dev returned 304 without a local cache")
        return _snapshot_from_cache(cached, stale=False)
    response.raise_for_status()
    snapshot, cache_envelope = _snapshot_from_response(response)
    try:
        _write_json_atomic(cache_path, cache_envelope)
    except OSError as exc:
        logger.warning("Unable to persist models.dev pricing cache: %s", exc)
    return snapshot


def _activate(snapshot: CatalogSnapshot) -> CatalogSnapshot:
    global _active_snapshot
    _context_snapshot.set(snapshot)
    with _catalog_lock:
        _active_snapshot = snapshot
    return snapshot


def activate_model_catalog(snapshot: CatalogSnapshot) -> None:
    """Bind a runner's frozen pricing snapshot to the current async context."""
    _context_snapshot.set(snapshot)


def _config_from_environment() -> PricingConfig:
    mode = os.getenv("SHINKA_PRICING_MODE", PricingMode.AUTO.value)
    cache_dir = os.getenv("SHINKA_CACHE_DIR")
    return PricingConfig(
        mode=mode,
        cache_dir=Path(cache_dir).expanduser() if cache_dir else None,
    )


def _cache_path(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        xdg_cache = os.getenv("XDG_CACHE_HOME")
        root = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
        cache_dir = root / "shinka-evolve"
    return Path(cache_dir).expanduser() / _CACHE_FILENAME


def _request_catalog(
    url: str,
    cached: dict[str, Any] | None,
    client: httpx.Client | None,
) -> httpx.Response:
    headers = {"User-Agent": MODELS_DEV_USER_AGENT, "Accept": "application/json"}
    if cached and cached.get("etag"):
        headers["If-None-Match"] = str(cached["etag"])
    if client is not None:
        return _stream_catalog(client, url, headers)
    timeout = httpx.Timeout(10.0, connect=3.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as owned_client:
        return _stream_catalog(owned_client, url, headers)


def _stream_catalog(
    client: httpx.Client, url: str, headers: dict[str, str]
) -> httpx.Response:
    started_at = time.monotonic()
    timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=3.0)
    with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        if response.status_code not in {200, 304}:
            response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > MAX_CATALOG_BYTES:
            raise ValueError("models.dev response exceeds the 10 MiB safety limit")
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() - started_at > FETCH_DEADLINE_SECONDS:
                raise httpx.TimeoutException("models.dev fetch exceeded 10 seconds")
            body.extend(chunk)
            if len(body) > MAX_CATALOG_BYTES:
                raise ValueError("models.dev response exceeds the 10 MiB safety limit")
        response_headers = dict(response.headers)
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)
        return httpx.Response(
            response.status_code,
            headers=response_headers,
            content=bytes(body),
            request=response.request,
        )


def _snapshot_from_response(
    response: httpx.Response,
) -> tuple[CatalogSnapshot, dict[str, Any]]:
    body = response.content
    payload = _loads_json(body)
    catalog = catalog_from_models_dev_payload(payload)
    fetched_at = datetime.now(timezone.utc).isoformat()
    digest = _payload_digest(payload)
    etag = response.headers.get("ETag")
    envelope = {
        "etag": etag,
        "fetched_at": fetched_at,
        "sha256": digest,
        "payload": payload,
    }
    return CatalogSnapshot(catalog, "live", fetched_at, etag, digest), envelope


def _snapshot_from_cache(envelope: dict[str, Any], *, stale: bool) -> CatalogSnapshot:
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Cached models.dev payload is invalid")
    return CatalogSnapshot(
        catalog=catalog_from_models_dev_payload(payload),
        source="cache",
        fetched_at=_optional_string(envelope.get("fetched_at")),
        etag=_optional_string(envelope.get("etag")),
        sha256=str(envelope.get("sha256") or _payload_digest(payload)),
        stale=stale,
    )


def _validated_cache(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    if envelope is None:
        return None
    try:
        payload = envelope.get("payload")
        if envelope.get("sha256") != _payload_digest(payload):
            raise ValueError("Cached models.dev payload digest mismatch")
        _snapshot_from_cache(envelope, stale=False)
    except ValueError:
        return None
    return envelope


def _bundled_snapshot(stale: bool) -> CatalogSnapshot:
    from .normalization import load_bundled_entries

    entries = load_bundled_entries()
    digest = _payload_digest([asdict(entry) for entry in entries])
    return CatalogSnapshot(
        PricingCatalog(entries), "bundled", None, None, digest, stale
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _payload_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _catalog_digest(entries: tuple[ModelPrice, ...]) -> str:
    return _payload_digest([asdict(entry) for entry in entries])


def _validate_entries(entries: tuple[ModelPrice, ...]) -> None:
    keys: set[tuple[str, str, str]] = set()
    for entry in entries:
        if entry.kind not in {"llm", "embedding"}:
            raise ValueError("invalid model kind in pricing snapshot")
        if not entry.model_name or not entry.api_model_name or not entry.provider:
            raise ValueError("empty model identity in pricing snapshot")
        for price in (
            entry.input_price,
            entry.output_price,
            entry.input_price_tier2,
            entry.output_price_tier2,
        ):
            if price is not None and (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or not math.isfinite(price)
                or price < 0
            ):
                raise ValueError("invalid price in pricing snapshot")
        if entry.kind == "llm" and (
            (entry.input_price is None) != (entry.output_price is None)
        ):
            raise ValueError("pricing snapshot has a partial price")
        if entry.kind == "embedding" and entry.output_price is not None:
            raise ValueError("embedding snapshot entry has an output price")
        if entry.tier_threshold is not None and (
            isinstance(entry.tier_threshold, bool)
            or not isinstance(entry.tier_threshold, int)
            or entry.tier_threshold <= 0
        ):
            raise ValueError("invalid tier threshold in pricing snapshot")
        if not all(
            isinstance(value, bool)
            for value in (
                entry.is_reasoning,
                entry.think_temp_fixed,
                entry.requires_reasoning,
            )
        ):
            raise ValueError("invalid model behavior in pricing snapshot")
        key = (entry.kind, entry.provider, entry.api_model_name)
        if key in keys:
            raise ValueError("duplicate model entry in pricing snapshot")
        keys.add(key)


def _loads_json(value: str | bytes) -> Any:
    return json.loads(value, parse_constant=_reject_json_constant)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        document = _loads_json(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
