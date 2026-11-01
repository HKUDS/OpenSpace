"""Cell-scoped host-side Chat Completions broker for isolated agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import select
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Self
from uuid import UUID

PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA = "rsi-arena.provider-reliability"
PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA = "rsi-arena.provider-trial"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROVIDER_CHAT_PATH = "/v1/chat/completions"
_OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

# One cell's whole allowance. These are sized from measured solver behaviour so
# defaults leave room for extended experiments. Effective limits are recorded
# in each trial; operators can disable the optional settled-cost gate.
_PROVIDER_MAX_REQUESTS = 4096
_PROVIDER_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_PROVIDER_MAX_TOTAL_REQUEST_BYTES = 1024 * 1024 * 1024
_PROVIDER_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_PROVIDER_MAX_TOTAL_RESPONSE_BYTES = 512 * 1024 * 1024
_PROVIDER_MAX_OUTPUT_TOKENS = 131_072
_PROVIDER_MAX_TOTAL_OUTPUT_TOKENS = 16_777_216
_PROVIDER_MAX_INFLIGHT_REQUESTS = 16
_PROVIDER_SOCKET_READ_TIMEOUT_SECONDS = 10.0
WORKER_PROVIDER_HOST = "127.0.0.1"
WORKER_PROVIDER_PORT = 18080
WORKER_PROVIDER_BASE_URL = f"http://{WORKER_PROVIDER_HOST}:{WORKER_PROVIDER_PORT}/v1"
WORKER_PROVIDER_SOCKET = "/rsi-arena/provider/provider.sock"
# One jittered reconnect absorbs a transient DNS/refused-connection failure
# without multiplying an outage into a retry storm.  Ambiguous failures such as
# timeout/reset are never retried: Chat Completions has no pinned idempotency
# contract here, so OpenRouter may already have accepted and billed that call.
_PROVIDER_UPSTREAM_RETRIES = 1
_PROVIDER_UPSTREAM_RETRY_BACKOFF_SECONDS = 1.0
PROVIDER_DIAGNOSTICS_NAME = "provider-diagnostics.json"
HOST_TMPDIR_ENV = "RSI_ARENA_HOST_TMPDIR"
_HOST_SOCKET_LAYOUTS = {
    "provider": ("rsi-arena-provider-", "provider.sock"),
    "program": ("rsi-arena-program-", "rpc/environment.sock"),
    "agent": ("rsi-hb-", "socket-parent/environment.sock"),
}


def host_temporary_directory(kind: str) -> TemporaryDirectory:
    """Private host storage independent of Harbor's long per-cell TMPDIR.

    The worker's /tmp remains its isolated tmpfs. Host socket paths must fit
    Linux sockaddr_un even when the experiment directory is deeply nested.
    """
    prefix, suffix = _HOST_SOCKET_LAYOUTS[kind]
    root = Path(os.environ.get(HOST_TMPDIR_ENV, "/tmp"))
    if not root.is_absolute():
        raise ValueError(f"{HOST_TMPDIR_ENV} must be an absolute directory")
    root = root.resolve(strict=True)
    directory = TemporaryDirectory(prefix=prefix, dir=root)
    if len(os.fsencode(Path(directory.name) / suffix)) >= 108:
        directory.cleanup()
        raise ValueError(f"{HOST_TMPDIR_ENV} is too long for a Unix socket; choose a shorter directory")
    return directory


def check_host_transport() -> dict[str, object]:
    """Exercise the real broker and both RPC socket layouts without model calls."""
    class NoUpstream:
        def open(self, *args, **kwargs):
            raise AssertionError("Transport preflight must not call a provider")
    broker = ScopedProviderBroker(provider_bearer="offline-transport-preflight",
        api_base=_OPENROUTER_API_BASE, model_name="openrouter/preflight/no-model", opener=NoUpstream())
    try:
        broker.start()
        root = broker.socket_path.parent.parent
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
        for kind in ("program", "agent"):
            with host_temporary_directory(kind) as directory:
                path = Path(directory) / _HOST_SOCKET_LAYOUTS[kind][1]
                path.parent.mkdir(mode=0o700)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    listener.listen(1)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(str(path))
                        connection, _ = listener.accept()
                        connection.close()
        return {"host_tmpdir": str(root), "socket_layouts": list(_HOST_SOCKET_LAYOUTS), "provider_calls": 0}
    finally:
        broker.close()

# Named so the isolated worker can tell one of our own ceilings from an upstream
# rate limit. Reaching ours ends the agent's turn, because the allowance is part
# of the measurement; an upstream refusal is a fault and must stay one.
BUDGET_EXHAUSTED_REQUESTS = "request budget exhausted"
BUDGET_EXHAUSTED_PAYLOAD = "provider budget exhausted"
SETTLED_COST_STOP_REACHED = "provider settled-cost stop reached"
BUDGET_EXHAUSTED_MARKERS = (
    BUDGET_EXHAUSTED_REQUESTS,
    BUDGET_EXHAUSTED_PAYLOAD,
    SETTLED_COST_STOP_REACHED,
)


class ScopedProviderBrokerError(RuntimeError):
    """The scoped provider relay rejected or could not serve a request."""


# Operator exclusion, including every endpoint variant. This relay is also
# deployed as a standalone stdlib module inside Harbor's isolated runtime.
DISABLED_OPENROUTER_PROVIDERS = ('deepinfra', 'parasail')
MODEL_DISABLED_OPENROUTER_PROVIDERS = {'qwen/qwen3.5-9b': ('together', 'darkbloom/fp4'),
 'qwen/qwen3.8-27b': ('dekallm',),
 'z-ai/glm-5.3-flash': ('baseten/fp8',
                        'cloudflare',
                        'coreweave/fp8',
                        'crusoe/fp4',
                        'digitalocean',
                        'fireworks',
                        'io-net/fp8',
                        'makora',
                        'modal/fp8',
                        'morph/fp8',
                        'nextbit/fp8',
                        'novita/fp8',
                        'phala/fp8',
                        'reka/fp8',
                        'streamlake/fp8',
                        'together',
                        'venice')}


def ignored_providers(model=None):
    model = model.removeprefix('openrouter/') if isinstance(model, str) else model
    return DISABLED_OPENROUTER_PROVIDERS + MODEL_DISABLED_OPENROUTER_PROVIDERS.get(model, ())


def provider_policy():
    return {'excluded_providers': list(DISABLED_OPENROUTER_PROVIDERS),
            'model_excluded_providers': {model: list(routes) for model, routes in MODEL_DISABLED_OPENROUTER_PROVIDERS.items()}}


def provider_routes(value: str | None, model: str | None = None) -> tuple[str, ...]:
    """Operator-only ordered allowlist; a single slug retains the pinned route."""
    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError("provider_only must contain comma-separated endpoint slugs")
    routes = tuple(value.split(','))
    if (not 1 <= len(routes) <= 8 or len(set(routes)) != len(routes)
            or any(re.fullmatch(r'[a-z0-9][a-z0-9/_-]{0,127}', x) is None for x in routes)):
        raise ValueError("provider_only must contain unique comma-separated endpoint slugs")
    blocked = [route for route in routes if any(route == banned or route.startswith(banned+'/')
               for banned in ignored_providers(model))]
    if blocked:
        raise ValueError('provider_only contains disabled provider: '+', '.join(blocked))
    return routes


def provider_route_body(value: str | None, attempt: int = 0, model: str | None = None) -> dict[str, object]:
    routes = provider_routes(value, model)
    if not routes:
        return {'ignore': list(ignored_providers(model))}
    order = routes[attempt % len(routes):] + routes[:attempt % len(routes)]
    result = {'only': list(routes), 'allow_fallbacks': len(routes) > 1, 'require_parameters': True,
              'ignore': list(ignored_providers(model))}
    if len(routes) > 1:
        result['order'] = list(order)
    return result


def service_refusal(status: int, body: bytes) -> bool:
    """Only a complete error without generated output can be resubmitted.

    This is dispatch eligibility, NOT a claim that the failed call cost zero.
    Timeouts, partial reads and errors accompanying model output are excluded.
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return status == 429
    if not isinstance(payload, dict):
        return False
    if payload.get('choices') or payload.get('output') or payload.get('response'):
        return False
    usage = payload.get('usage') or {}
    if not isinstance(usage, dict) or any(usage.get(k, 0) for k in ('completion_tokens', 'output_tokens')):
        return False
    error = payload.get('error')
    code = error.get('code') if isinstance(error, dict) else status
    return (status == 429 or 500 <= status <= 599 or
            type(code) is int and (code == 429 or 500 <= code <= 599))


def service_retry_delay(headers, attempt: int, *, random_source=None, now=None) -> float:
    """Honor seconds and HTTP-date Retry-After; never shorten a server cooldown."""
    from email.utils import parsedate_to_datetime
    retry_after = headers.get('Retry-After')
    delay = 0.0
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - (time.time() if now is None else now)
            except (TypeError, ValueError, OverflowError):
                delay = 0.0
        if not math.isfinite(delay):
            delay = 0.0
    jitter = (random_source or random).uniform(1., 1.25)
    return max(0.0, delay, min(30., 2. ** attempt) * jitter)


def provider_interfered(diagnostics: Mapping[str, Any] | None, *, timed_out: bool = False) -> bool:
    """Unrecovered upstream errors invalidate a trial, including mixed traffic.

    A relay recovery is usable only if its final answer reached the same request
    group. A deadline after any recovery remains inconclusive: waiting may have
    consumed time the candidate needed. Ordinary wrong answers are unaffected.
    """
    if not isinstance(diagnostics, Mapping):
        return False
    calls = diagnostics.get('calls') or ()
    local_stops = (diagnostics.get('provider') or {}).get('local_rejection_counts') or {}
    recovery_consumed_budget = any(local_stops.get(name, 0) for name in
                                   ('payload_budget', 'request_budget'))
    recovered = {c.get('request_group') for c in calls if isinstance(c, Mapping)
                 and c.get('request_group') is not None and c.get('status') == 'completed'
                 and type(c.get('http_status')) is int and 200 <= c['http_status'] < 300
                 and c.get('provider_error_code') is None and not c.get('retryable_refusal')}
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        codes = (call.get('http_status'), call.get('provider_error_code'))
        failed = call.get('status') == 'transport_failed' or any(
            type(code) is int and (code in {408, 429} or 500 <= code <= 599) for code in codes)
        if failed and (not call.get('retryable_refusal') or call.get('request_group') not in recovered
                       or timed_out or diagnostics.get('worker_deadline_reached') or recovery_consumed_budget):
            return True
    return False


def provider_calls_all_service_refused(diagnostics: Mapping[str, Any] | None) -> bool:
    """A pure service refusal is not an observed solver failure.

    Length exhaustion, bad candidate requests and missing telemetry do not
    establish this condition. Mixed successful/failed traffic remains outside
    this deliberately narrow rule.
    """
    calls = diagnostics.get("calls") if isinstance(diagnostics, Mapping) else None
    def refused(code):
        return type(code) is int and (code in {408,429} or 500 <= code <= 599)
    return isinstance(calls, (list, tuple)) and bool(calls) and all(
        isinstance(call, Mapping)
        and call.get("status") == "completed"
        and (refused(call.get('http_status')) or refused(call.get('provider_error_code')))
        for call in calls
    )


@dataclass(frozen=True)
class ProviderReliabilitySnapshot:
    """Bounded, content-free diagnostics for one cell's provider traffic.

    Durations and counters are safe to persist with a trial: they contain no
    prompts, responses, provider credentials, routing headers, or request IDs.
    ``open_requests`` is sampled rather than inferred from failures, which is
    important when the worker deadline interrupts an upstream call before the
    socket timeout can report that failure.
    """

    schema: str
    logical_requests_started: int
    logical_requests_finished: int
    upstream_attempts_started: int
    upstream_attempts_finished: int
    responses_started: int
    responses_finished: int
    transport_failures: int
    terminal_transport_failures: int
    safe_retries: int
    open_requests: int
    open_without_response: int
    open_reading_response: int
    oldest_open_request_seconds: float | None
    oldest_open_attempt_seconds: float | None
    total_upstream_wait_seconds: float
    terminal_transport_wait_seconds: float
    # Legacy name: failed transports plus long pending waits; pending generation
    # is unobserved, so this field does not establish that no useful work occurs.
    nonproductive_wait_seconds: float
    first_byte_samples: int
    first_byte_total_seconds: float
    first_byte_max_seconds: float | None
    last_progress_age_seconds: float | None
    last_transport_failure_age_seconds: float | None
    last_terminal_transport_failure_age_seconds: float | None
    upstream_timeout_seconds: float
    local_rejections: int
    local_rejection_counts: dict[str, int]
    last_rejected_fields: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderAccountingSnapshot:
    """Trusted content-free accounting observed at the provider boundary."""

    requests: int
    request_bytes: int
    response_bytes: int
    input_tokens: int
    cache_tokens: int
    output_tokens: int
    cost_usd: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderCallAccounting:
    """One content-free logical provider-call fact.

    The ordinal is allocated immediately before the first upstream attempt and
    never changes across a safe transport retry.  Missing token or cost fields
    stay null: neither a request ceiling nor an aggregate may be presented as a
    provider observation for this call.
    """

    ordinal: int
    status: str
    response_id: str | None
    generation_id: str | None
    input_tokens: int | None
    cache_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    http_status: int | None = None
    provider_error_code: int | None = None
    started_at: str | None = None
    elapsed_seconds: float | None = None
    request_max_tokens: int | None = None
    request_max_tokens_source: str | None = None
    request_reasoning: dict[str, object] | None = None
    response_artifact: str | None = None
    response_sha256: str | None = None
    response_complete: bool | None = None
    request_group: int | None = None
    retryable_refusal: bool = False
    configured_route: str | None = None
    provider_name: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _ProviderCallRecord:
    ordinal: int
    status: str = "in_flight"
    response_id: str | None = None
    generation_id: str | None = None
    input_tokens: int | None = None
    cache_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    http_status: int | None = None
    provider_error_code: int | None = None
    started_at: str | None = None
    elapsed_seconds: float | None = None
    request_max_tokens: int | None = None
    request_max_tokens_source: str | None = None
    request_reasoning: dict[str, object] | None = None
    response_artifact: str | None = None
    response_sha256: str | None = None
    response_complete: bool | None = None
    request_group: int | None = None
    retryable_refusal: bool = False
    configured_route: str | None = None
    provider_name: str | None = None

    def snapshot(self) -> ProviderCallAccounting:
        return ProviderCallAccounting(**asdict(self))


@dataclass
class _ActiveUpstreamRequest:
    started_at: float
    attempt_started_at: float
    phase: str = "opening"
    attempt: int = 1


def _interval_union_seconds(intervals: tuple[tuple[float, float], ...]) -> float:
    """Return the wall-clock union of bounded monotonic intervals."""

    normalized = sorted((start, end) for start, end in intervals if end >= start)
    if not normalized:
        return 0.0
    total = 0.0
    merged_start, merged_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= merged_end:
            merged_end = max(merged_end, end)
            continue
        total += merged_end - merged_start
        merged_start, merged_end = start, end
    return total + merged_end - merged_start


def provider_wait_dominated_deadline(
    snapshot: ProviderReliabilitySnapshot, *, worker_timeout_seconds: float
) -> bool:
    """Require completed transport failures before attributing a deadline.

    An unfinished non-streaming call may be generating useful reasoning. Its
    age, and the absence of response bytes, cannot establish an upstream fault.
    Keep pending calls in the accounting journal but do not use them to excuse
    a task timeout. The legacy ``nonproductive_wait_seconds`` telemetry also
    includes pending waits and is therefore not a causal classification input.
    """

    if not isinstance(snapshot, ProviderReliabilitySnapshot):
        raise TypeError("provider reliability snapshot is required")
    if (
        isinstance(worker_timeout_seconds, bool)
        or not isinstance(worker_timeout_seconds, (int, float))
        or not math.isfinite(float(worker_timeout_seconds))
        or worker_timeout_seconds <= 0
    ):
        raise ValueError("worker timeout must be a positive finite number")
    recent_failure_window = min(300.0, snapshot.upstream_timeout_seconds * 2.0)
    recent_failure = (
        snapshot.terminal_transport_failures > 0
        and snapshot.last_terminal_transport_failure_age_seconds is not None
        and snapshot.last_terminal_transport_failure_age_seconds
        <= recent_failure_window
    )
    failed_transport_fraction = min(
        1.0,
        snapshot.terminal_transport_wait_seconds / float(worker_timeout_seconds),
    )
    return recent_failure and failed_transport_fraction >= 0.5


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validate_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _CONTROL.search(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _validate_api_base(value: str | None) -> str | None:
    if value is None:
        return None
    url = _validate_text(value, "api_base", maximum=4096)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("api_base must be an HTTP(S) URL without credentials")
    return url


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url


class _ScopedProviderBrokerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rsi-arena-provider-broker"
    sys_version = ""

    def do_POST(self) -> None:
        broker = self.server.broker  # type: ignore[attr-defined]
        broker.handle(self)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _BoundedServerMixin:
    client_read_timeout_seconds: float

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        connection.settimeout(self.client_read_timeout_seconds)
        broker = getattr(self, "broker", None)
        if broker is not None:
            broker._track_socket(connection)
        return connection, address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        broker = self.broker  # type: ignore[attr-defined]
        if not broker._acquire_connection_slot():
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            broker._release_connection_slot()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: Any
    ) -> None:
        broker = self.broker  # type: ignore[attr-defined]
        try:
            super().process_request_thread(request, client_address)
        finally:
            broker._release_connection_slot()

    def shutdown_request(self, request: socket.socket) -> None:
        try:
            super().shutdown_request(request)
        finally:
            broker = getattr(self, "broker", None)
            if broker is not None:
                broker._untrack_socket(request)


class _BoundedThreadingHTTPServer(_BoundedServerMixin, ThreadingHTTPServer):
    pass


class _BoundedThreadingUnixHTTPServer(
    _BoundedServerMixin,
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer,
):
    daemon_threads = True
    block_on_close = False


class _UnixRelayHandler(socketserver.BaseRequestHandler):
    """Copy one loopback TCP connection to the mounted host UDS."""

    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(self.server.socket_timeout_seconds)  # type: ignore[attr-defined]
            upstream.connect(self.server.unix_socket_path)  # type: ignore[attr-defined]
            self.request.settimeout(self.server.socket_timeout_seconds)  # type: ignore[attr-defined]
            sockets = (self.request, upstream)
            while True:
                readable, _, _ = select.select(sockets, (), (), 1.0)
                if not readable:
                    if self.server.stopping.is_set():  # type: ignore[attr-defined]
                        return
                    continue
                for source in readable:
                    try:
                        payload = source.recv(64 * 1024)
                    except (OSError, TimeoutError):
                        return
                    if not payload:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(payload)
        finally:
            upstream.close()


class UnixProviderRelay:
    """Candidate-side loopback adapter for a host-owned Unix broker socket.

    The relay carries no provider credential and performs no policy decisions.
    It exists solely because provider SDKs require an HTTP URL rather than an
    AF_UNIX path.  The surrounding worker must run in a private network
    namespace; therefore this loopback listener is its only TCP route.
    """

    def __init__(
        self,
        unix_socket_path: str = WORKER_PROVIDER_SOCKET,
        *,
        host: str = WORKER_PROVIDER_HOST,
        port: int = WORKER_PROVIDER_PORT,
        socket_timeout_seconds: float = 10.0,
    ) -> None:
        if host != WORKER_PROVIDER_HOST or port not in {0, WORKER_PROVIDER_PORT}:
            raise ValueError("worker provider relay address is fixed")
        if not isinstance(unix_socket_path, str) or not unix_socket_path.startswith(
            "/"
        ):
            raise ValueError("worker provider socket must be absolute")
        self._stopping = threading.Event()
        server_type = type(
            "_WorkerUnixProviderRelayServer",
            (socketserver.ThreadingMixIn, socketserver.TCPServer),
            {"allow_reuse_address": True, "daemon_threads": True},
        )
        self._server = server_type((host, port), _UnixRelayHandler)
        self._server.unix_socket_path = unix_socket_path
        self._server.socket_timeout_seconds = float(socket_timeout_seconds)
        self._server.stopping = self._stopping
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="rsi-arena-provider-relay",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class ScopedProviderBroker:
    """Cell-scoped provider relay that never exposes the long-lived key.

    Candidate code receives only a random bearer which dies with this broker.
    Every forwarded request is pinned to one model and reserves bounded request,
    response, and output-token budgets before provider I/O.
    """

    error_type: type[Exception] = ScopedProviderBrokerError

    def __init__(
        self,
        *,
        provider_bearer: str,
        api_base: str,
        model_name: str,
        maximum_requests: int = _PROVIDER_MAX_REQUESTS,
        maximum_request_bytes: int = _PROVIDER_MAX_REQUEST_BYTES,
        maximum_total_request_bytes: int = _PROVIDER_MAX_TOTAL_REQUEST_BYTES,
        maximum_response_bytes: int = _PROVIDER_MAX_RESPONSE_BYTES,
        maximum_total_response_bytes: int = _PROVIDER_MAX_TOTAL_RESPONSE_BYTES,
        maximum_output_tokens: int = _PROVIDER_MAX_OUTPUT_TOKENS,
        maximum_total_output_tokens: int = _PROVIDER_MAX_TOTAL_OUTPUT_TOKENS,
        maximum_inflight_requests: int = _PROVIDER_MAX_INFLIGHT_REQUESTS,
        settled_cost_stop_usd: float | None = None,
        socket_read_timeout_seconds: float = _PROVIDER_SOCKET_READ_TIMEOUT_SECONDS,
        upstream_retries: int = _PROVIDER_UPSTREAM_RETRIES,
        upstream_retry_backoff_seconds: float = (
            _PROVIDER_UPSTREAM_RETRY_BACKOFF_SECONDS
        ),
        timeout_seconds: float = 1_200.0,
        provider_only: str | None = None,
        response_artifact_dir: Path | None = None,
        opener: Any | None = None,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
        random_source: Any | None = None,
        service_retries: int | None = None,
        service_retry_window_seconds: float = 180.,
    ) -> None:
        if (
            not isinstance(provider_bearer, str)
            or not 4 <= len(provider_bearer) <= 65_536
            or _CONTROL.search(provider_bearer)
        ):
            raise self.error_type("provider credential is invalid")
        routes = provider_routes(provider_only, model_name)
        self._provider_only = provider_only
        self._route_offset = 0
        self._service_retries = (3 if routes else 0) if service_retries is None else service_retries
        if type(self._service_retries) is not int or not 0 <= self._service_retries <= 8:
            raise ValueError('service_retries must be in [0,8]')
        if type(service_retry_window_seconds) not in (int, float) or not 0 <= service_retry_window_seconds <= 600:
            raise ValueError('service_retry_window_seconds must be in [0,600]')
        self._service_retry_window = service_retry_window_seconds
        self._service_exhausted = False
        validated_base = _validate_api_base(api_base)
        if validated_base != _OPENROUTER_API_BASE:
            raise self.error_type(
                "provider api_base must equal the pinned OpenRouter endpoint"
            )
        parsed_base = urllib.parse.urlsplit(validated_base)
        if (
            parsed_base.scheme != "https"
            or parsed_base.query
            or parsed_base.fragment
            or any(part in {".", ".."} for part in parsed_base.path.split("/"))
        ):
            raise self.error_type("provider api_base must be a fixed HTTPS endpoint")
        model = _validate_text(model_name, "model_name", maximum=512)
        if not model.startswith("openrouter/") or len(model) == len("openrouter/"):
            raise self.error_type(
                "scoped provider broker requires an openrouter-prefixed model"
            )
        limits = (
            maximum_requests,
            maximum_request_bytes,
            maximum_total_request_bytes,
            maximum_response_bytes,
            maximum_total_response_bytes,
            maximum_output_tokens,
            maximum_total_output_tokens,
            maximum_inflight_requests,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in limits
        ):
            raise ValueError("scoped provider broker limits must be positive integers")
        if maximum_total_request_bytes < maximum_request_bytes:
            raise ValueError("total request bytes must cover one request")
        if maximum_total_response_bytes < maximum_response_bytes:
            raise ValueError("total response bytes must cover one response")
        if maximum_total_output_tokens < maximum_output_tokens:
            raise ValueError("total output tokens must cover one request")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0 < float(timeout_seconds) <= 86_400
        ):
            raise ValueError("scoped provider timeout must be in (0, 86400]")
        if (
            isinstance(socket_read_timeout_seconds, bool)
            or not isinstance(socket_read_timeout_seconds, (int, float))
            or not math.isfinite(float(socket_read_timeout_seconds))
            or not 0 < float(socket_read_timeout_seconds) <= 30
        ):
            raise ValueError("broker socket read timeout must be in (0, 30]")
        if (
            isinstance(upstream_retries, bool)
            or not isinstance(upstream_retries, int)
            or not 0 <= upstream_retries <= 2
        ):
            raise ValueError("upstream retries must be in [0, 2]")
        if (
            isinstance(upstream_retry_backoff_seconds, bool)
            or not isinstance(upstream_retry_backoff_seconds, (int, float))
            or not math.isfinite(float(upstream_retry_backoff_seconds))
            or not 0 <= float(upstream_retry_backoff_seconds) <= 30
        ):
            raise ValueError("upstream retry backoff must be in [0, 30]")
        if settled_cost_stop_usd is not None and (
            isinstance(settled_cost_stop_usd, bool)
            or not isinstance(settled_cost_stop_usd, (int, float))
            or not math.isfinite(float(settled_cost_stop_usd))
            or float(settled_cost_stop_usd) < 0.0
        ):
            raise ValueError("settled_cost_stop_usd must be finite and non-negative")

        upstream_path = parsed_base.path.rstrip("/") + "/chat/completions"
        self._upstream_url = urllib.parse.urlunsplit(
            (
                parsed_base.scheme,
                parsed_base.netloc,
                upstream_path,
                "",
                "",
            )
        )
        self._allowed_model = model.removeprefix("openrouter/")
        self._provider_bearer: str | None = provider_bearer
        self._ephemeral_bearer: str | None = secrets.token_urlsafe(32)
        self._maximum_requests = maximum_requests
        self._maximum_request_bytes = maximum_request_bytes
        self._maximum_total_request_bytes = maximum_total_request_bytes
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_total_response_bytes = maximum_total_response_bytes
        self._maximum_output_tokens = maximum_output_tokens
        self._maximum_total_output_tokens = maximum_total_output_tokens
        self._maximum_inflight_requests = maximum_inflight_requests
        self._settled_cost_stop_usd = (
            None if settled_cost_stop_usd is None else float(settled_cost_stop_usd)
        )
        self._timeout_seconds = float(timeout_seconds)
        self._response_artifact_dir = response_artifact_dir
        self._response_artifact_dir_fd: int | None = None
        if response_artifact_dir is not None:
            if not response_artifact_dir.is_absolute():
                raise ValueError(
                    "provider response artifact directory must be absolute"
                )
            try:
                response_artifact_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
                self._response_artifact_dir_fd = os.open(
                    response_artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except OSError:
                # Optional audit storage must never prevent a solver call.
                self._response_artifact_dir = None
        self._upstream_retries = int(upstream_retries)
        self._upstream_retry_backoff = float(upstream_retry_backoff_seconds)
        if not callable(clock) or not callable(sleeper):
            raise TypeError("broker clock and sleeper must be callable")
        self._clock = clock
        self._sleeper = sleeper
        self._random = random_source or random.SystemRandom()
        if not callable(getattr(self._random, "uniform", None)):
            raise TypeError("broker random source must provide uniform()")
        self._logical_requests_started = 0
        self._logical_requests_finished = 0
        self._upstream_attempts_started = 0
        self._upstream_attempts_finished = 0
        self._responses_started = 0
        self._responses_finished = 0
        self._transport_failures = 0
        self._terminal_transport_failures = 0
        self._safe_retries = 0
        self._total_upstream_wait_seconds = 0.0
        self._terminal_transport_intervals: list[tuple[float, float]] = []
        self._first_byte_samples = 0
        self._first_byte_total_seconds = 0.0
        self._first_byte_max_seconds: float | None = None
        self._last_progress: float | None = None
        self._last_transport_failure: float | None = None
        self._last_terminal_transport_failure: float | None = None
        self._next_upstream_request_id = 1
        self._active_upstream_requests: dict[int, _ActiveUpstreamRequest] = {}
        self._provider_calls: dict[int, _ProviderCallRecord] = {}
        self._journal_fds: dict[int, int] = {}
        self._quiescing = False
        self._local_rejections = 0
        self._local_rejection_counts: dict[str, int] = {}
        self._last_rejected_fields: tuple[str, ...] = ()
        self._request_count = 0
        self._request_bytes = 0
        self._response_bytes = 0
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0
        self._cost_reported_for_all_responses = True
        self._reserved_output_tokens = 0
        self._reserved_response_bytes = 0
        self._active_connection_count = 0
        self._lock = threading.RLock()
        self._connection_slots = threading.BoundedSemaphore(maximum_inflight_requests)
        self._active_sockets: set[socket.socket] = set()
        self._socket_directory = host_temporary_directory("provider")
        self._socket_path = Path(self._socket_directory.name) / "provider.sock"
        self._server = _BoundedThreadingUnixHTTPServer(
            str(self._socket_path), _ScopedProviderBrokerHandler
        )
        self._socket_path.chmod(0o600)
        self._server.client_read_timeout_seconds = float(socket_read_timeout_seconds)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._server.broker = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="rsi-arena-provider-broker",
            daemon=True,
        )
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise TypeError("broker opener must provide open()")
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())
        self._started = False
        self._closed = False

    @property
    def base_url(self) -> str:
        return WORKER_PROVIDER_BASE_URL

    @property
    def socket_path(self) -> Path:
        if self._closed:
            raise self.error_type("scoped provider broker is closed")
        return self._socket_path

    def accounting_snapshot(self) -> ProviderAccountingSnapshot:
        with self._lock:
            return ProviderAccountingSnapshot(
                requests=self._request_count,
                request_bytes=self._request_bytes,
                response_bytes=self._response_bytes,
                input_tokens=self._input_tokens,
                cache_tokens=self._cache_tokens,
                output_tokens=self._output_tokens,
                cost_usd=(
                    self._cost_usd
                    if self._cost_reported_for_all_responses
                    and self._responses_finished > 0
                    and all(
                        record.status == "completed"
                        for record in self._provider_calls.values()
                    )
                    else None
                ),
            )

    def call_accounting_snapshot(self) -> tuple[ProviderCallAccounting, ...]:
        """Return stable-ordinal, content-free facts for real upstream calls."""

        with self._lock:
            now = self._clock()
            for ordinal, active in self._active_upstream_requests.items():
                self._provider_calls[ordinal].elapsed_seconds = round(
                    max(0.0, now - active.started_at), 6
                )
            return tuple(
                self._provider_calls[ordinal].snapshot()
                for ordinal in sorted(self._provider_calls)
            )

    def trial_diagnostics_snapshot(
        self,
    ) -> tuple[
        ProviderReliabilitySnapshot,
        ProviderAccountingSnapshot,
        tuple[ProviderCallAccounting, ...],
    ]:
        """Capture the mutually validated sidecar fields without interleaving."""

        with self._lock:
            return (
                self.reliability_snapshot(),
                self.accounting_snapshot(),
                self.call_accounting_snapshot(),
            )

    def reliability_snapshot(self) -> ProviderReliabilitySnapshot:
        """Return one internally consistent, bounded telemetry sample."""

        now = self._clock()
        with self._lock:
            active = tuple(self._active_upstream_requests.values())
            open_ages = tuple(max(0.0, now - item.started_at) for item in active)
            attempt_ages = tuple(
                max(0.0, now - item.attempt_started_at) for item in active
            )
            minimum_stall_age = max(5.0, min(90.0, self._timeout_seconds * 0.75))
            active_stall_intervals = tuple(
                (item.attempt_started_at, now)
                for item in active
                if item.phase in {"opening", "reading_response"}
                and now - item.attempt_started_at >= minimum_stall_age
            )
            terminal_intervals = tuple(self._terminal_transport_intervals)
            terminal_union = _interval_union_seconds(terminal_intervals)
            nonproductive_union = _interval_union_seconds(
                terminal_intervals + active_stall_intervals
            )
            active_wait = sum(open_ages)
            return ProviderReliabilitySnapshot(
                schema=PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA,
                logical_requests_started=self._logical_requests_started,
                logical_requests_finished=self._logical_requests_finished,
                upstream_attempts_started=self._upstream_attempts_started,
                upstream_attempts_finished=self._upstream_attempts_finished,
                responses_started=self._responses_started,
                responses_finished=self._responses_finished,
                transport_failures=self._transport_failures,
                terminal_transport_failures=self._terminal_transport_failures,
                safe_retries=self._safe_retries,
                open_requests=len(active),
                open_without_response=sum(
                    item.phase in {"opening", "retry_backoff"} for item in active
                ),
                open_reading_response=sum(
                    item.phase == "reading_response" for item in active
                ),
                oldest_open_request_seconds=(
                    round(max(open_ages), 3) if open_ages else None
                ),
                oldest_open_attempt_seconds=(
                    round(max(attempt_ages), 3) if attempt_ages else None
                ),
                total_upstream_wait_seconds=round(
                    self._total_upstream_wait_seconds + active_wait, 3
                ),
                terminal_transport_wait_seconds=round(terminal_union, 3),
                nonproductive_wait_seconds=round(nonproductive_union, 3),
                first_byte_samples=self._first_byte_samples,
                first_byte_total_seconds=round(self._first_byte_total_seconds, 3),
                first_byte_max_seconds=(
                    None
                    if self._first_byte_max_seconds is None
                    else round(self._first_byte_max_seconds, 3)
                ),
                last_progress_age_seconds=(
                    None
                    if self._last_progress is None
                    else round(max(0.0, now - self._last_progress), 3)
                ),
                last_transport_failure_age_seconds=(
                    None
                    if self._last_transport_failure is None
                    else round(max(0.0, now - self._last_transport_failure), 3)
                ),
                last_terminal_transport_failure_age_seconds=(
                    None
                    if self._last_terminal_transport_failure is None
                    else round(max(0.0, now - self._last_terminal_transport_failure), 3)
                ),
                upstream_timeout_seconds=self._timeout_seconds,
                local_rejections=self._local_rejections,
                local_rejection_counts=dict(
                    sorted(self._local_rejection_counts.items())
                ),
                last_rejected_fields=self._last_rejected_fields,
            )

    @property
    def ephemeral_bearer(self) -> str:
        bearer = self._ephemeral_bearer
        if bearer is None:
            raise self.error_type("scoped provider broker is closed")
        return bearer

    def start(self) -> None:
        if self._closed or self._started:
            raise self.error_type("scoped provider broker cannot be started")
        self._started = True
        self._thread.start()

    @staticmethod
    def _reply(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        token = self._ephemeral_bearer
        values = handler.headers.get_all("Authorization", failobj=[])
        return (
            not self._closed
            and token is not None
            and len(values) == 1
            and hmac.compare_digest(values[0], f"Bearer {token}")
        )

    def _validate_json_tree(
        self,
        value: object,
        *,
        label: str,
        maximum_depth: int = 16,
        maximum_nodes: int = 4096,
    ) -> None:
        remaining = maximum_nodes

        def visit(node: object, depth: int) -> None:
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > maximum_depth:
                raise self.error_type(f"{label} exceeds its structural bound")
            if node is None or isinstance(node, (bool, int, float)):
                if isinstance(node, float) and not math.isfinite(node):
                    raise self.error_type(f"{label} contains a non-finite number")
                return
            if isinstance(node, str):
                if len(node) > _PROVIDER_MAX_REQUEST_BYTES or "\x00" in node:
                    raise self.error_type(f"{label} contains invalid text")
                return
            if isinstance(node, list):
                if len(node) > 1024:
                    raise self.error_type(f"{label} contains an oversized list")
                for item in node:
                    visit(item, depth + 1)
                return
            if isinstance(node, dict):
                if len(node) > 512:
                    raise self.error_type(f"{label} contains an oversized object")
                for key, item in node.items():
                    if not isinstance(key, str) or len(key) > 512 or "\x00" in key:
                        raise self.error_type(f"{label} contains an invalid key")
                    visit(item, depth + 1)
                return
            raise self.error_type(f"{label} contains a non-JSON value")

        visit(value, 0)

    def _validation_error(
        self,
        message: str,
        *,
        reason: str = "request_validation",
        fields: tuple[str, ...] = (),
    ) -> Exception:
        """Build a validation error carrying only bounded, content-free metadata."""

        exc = self.error_type(message)
        exc.broker_rejection_reason = reason
        exc.broker_rejected_fields = fields[:16]
        return exc

    def _record_local_rejection(
        self, reason: str, *, fields: tuple[str, ...] = ()
    ) -> None:
        allowed_reasons = {
            "authorization",
            "content_length",
            "content_type",
            "settled_cost_stop",
            "incomplete_body",
            "model_mismatch",
            "payload_budget",
            "request_body_timeout",
            "request_budget",
            "request_size",
            "request_validation",
            "route",
            "tool_mismatch",
            "transfer_encoding",
            "unsupported_fields",
        }
        normalized_reason = (
            reason if reason in allowed_reasons else "request_validation"
        )
        safe_fields = tuple(
            sorted(
                {
                    field
                    for field in fields
                    if isinstance(field, str)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", field)
                }
            )[:16]
        )
        with self._lock:
            self._local_rejections += 1
            self._local_rejection_counts[normalized_reason] = (
                self._local_rejection_counts.get(normalized_reason, 0) + 1
            )
            self._last_rejected_fields = safe_fields

    def _validate_tools(self, value: object) -> None:
        if not isinstance(value, list) or not 1 <= len(value) <= 128:
            raise self._validation_error(
                "provider tools must be a bounded list", reason="tool_mismatch"
            )
        for tool in value:
            if not isinstance(tool, dict) or set(tool) != {"type", "function"}:
                raise self._validation_error(
                    "provider tool shape is invalid", reason="tool_mismatch"
                )
            function = tool.get("function")
            if tool.get("type") != "function" or not isinstance(function, dict):
                raise self._validation_error(
                    "provider tool must describe a function", reason="tool_mismatch"
                )
            if not {"name", "parameters"}.issubset(function) or not set(function) <= {
                "name",
                "description",
                "parameters",
                "strict",
            }:
                raise self._validation_error(
                    "provider function tool fields are invalid", reason="tool_mismatch"
                )
            name = function.get("name")
            description = function.get("description", "")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", name) is None
                or not isinstance(description, str)
                or len(description) > 16_384
                or "\x00" in description
                or (
                    "strict" in function
                    and not isinstance(function.get("strict"), bool)
                )
            ):
                raise self._validation_error(
                    "provider function tool metadata is invalid", reason="tool_mismatch"
                )
            parameters = function.get("parameters")
            if not isinstance(parameters, dict):
                raise self._validation_error(
                    "provider function parameters must be an object",
                    reason="tool_mismatch",
                )
            self._validate_json_tree(parameters, label="provider tool schema")

    def _validate_tool_choice(self, value: object, tool_names: set[str]) -> None:
        if isinstance(value, str) and value in {"auto", "none", "required"}:
            return
        if not isinstance(value, dict) or set(value) != {"type", "function"}:
            raise self._validation_error(
                "provider tool_choice is invalid", reason="tool_mismatch"
            )
        function = value.get("function")
        if (
            value.get("type") != "function"
            or not isinstance(function, dict)
            or set(function) != {"name"}
            or function.get("name") not in tool_names
        ):
            raise self._validation_error(
                "provider tool_choice is not a declared function",
                reason="tool_mismatch",
            )

    def _validated_request(self, body: bytes) -> tuple[bytes, int]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self.error_type("provider request must be JSON") from exc
        if not isinstance(value, dict):
            raise self.error_type("provider request must be an object")
        allowed_fields = {
            "model",
            "messages",
            "frequency_penalty",
            "logit_bias",
            "logprobs",
            "max_tokens",
            "max_completion_tokens",
            "n",
            "parallel_tool_calls",
            "presence_penalty",
            "reasoning",
            "response_format",
            "seed",
            "session_id",
            "stop",
            "stream",
            "temperature",
            "tool_choice",
            "tools",
            "top_logprobs",
            "top_p",
            "usage",
            "user",
        }
        unknown = set(value) - allowed_fields
        if unknown:
            raise self._validation_error(
                "provider request contains an unsupported field",
                reason="unsupported_fields",
                fields=tuple(sorted(unknown)),
            )
        if value.get("model") != self._allowed_model:
            raise self._validation_error(
                "provider request model differs from cell model",
                reason="model_mismatch",
            )
        messages = value.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 1024:
            raise self.error_type("provider messages must be a bounded list")
        self._validate_json_tree(messages, label="provider messages")
        if "session_id" in value:
            session_id = value["session_id"]
            try:
                parsed_session_id = (
                    UUID(session_id) if isinstance(session_id, str) else None
                )
            except ValueError:
                parsed_session_id = None
            if (
                parsed_session_id is None
                or parsed_session_id.version != 4
                or str(parsed_session_id) != session_id
            ):
                raise self._validation_error("provider session_id is invalid")
        if "usage" in value and value["usage"] != {"include": True}:
            raise self._validation_error("provider usage request is invalid")
        if value.get("stream", False) is not False:
            raise self.error_type("streaming provider requests are disabled")
        tools = value.get("tools")
        if tools is not None:
            self._validate_tools(tools)
            tool_names = {tool["function"]["name"] for tool in tools}
            if "tool_choice" in value:
                self._validate_tool_choice(value["tool_choice"], tool_names)
        elif "tool_choice" in value or "parallel_tool_calls" in value:
            raise self.error_type("provider tool controls require declared tools")
        if "parallel_tool_calls" in value and not isinstance(
            value["parallel_tool_calls"], bool
        ):
            raise self.error_type("parallel_tool_calls must be boolean")
        for name in ("n", "best_of"):
            multiplicity = value.get(name, 1)
            if (
                isinstance(multiplicity, bool)
                or not isinstance(multiplicity, int)
                or multiplicity != 1
            ):
                raise self.error_type("provider request multiplicity must equal one")
        present = [
            name for name in ("max_tokens", "max_completion_tokens") if name in value
        ]
        if len(present) > 1:
            raise self.error_type("provider output-token bound is ambiguous")
        if present:
            requested_tokens = value[present[0]]
            if (
                isinstance(requested_tokens, bool)
                or not isinstance(requested_tokens, int)
                or not 1 <= requested_tokens <= self._maximum_output_tokens
            ):
                raise self.error_type("provider output-token bound is invalid")
        else:
            requested_tokens = self._maximum_output_tokens
            value["max_tokens"] = requested_tokens
        value["provider"] = provider_route_body(self._provider_only, self._route_offset, self._allowed_model)
        normalized = _canonical_bytes(value)
        if len(normalized) > self._maximum_request_bytes:
            raise self.error_type("provider request exceeds its byte bound")
        provider_bearer = self._provider_bearer
        if provider_bearer is None or provider_bearer.encode("utf-8") in normalized:
            raise self.error_type("provider request crossed credential boundary")
        return normalized, requested_tokens

    def _acquire_connection_slot(self) -> bool:
        if not self._connection_slots.acquire(blocking=False):
            return False
        with self._lock:
            if self._closed:
                self._connection_slots.release()
                return False
            self._active_connection_count += 1
        return True

    def _release_connection_slot(self) -> None:
        with self._lock:
            self._active_connection_count -= 1
            if self._active_connection_count < 0:
                raise RuntimeError("program connection-slot invariant failed")
        self._connection_slots.release()

    def _track_socket(self, connection: socket.socket) -> None:
        with self._lock:
            if not self._closed:
                self._active_sockets.add(connection)

    def _untrack_socket(self, connection: socket.socket) -> None:
        with self._lock:
            self._active_sockets.discard(connection)

    def _claim_request(self) -> str:
        with self._lock:
            if self._closed or self._request_count + 1 > self._maximum_requests:
                return "request_budget"
            if self._quiescing:
                return "request_budget"
            if self._service_exhausted:
                return "request_budget"
            settled_calls = tuple(
                record
                for record in self._provider_calls.values()
                if record.status != "in_flight"
            )
            if self._settled_cost_stop_usd is not None and settled_calls:
                # Unknown invoices remain unknown in the receipt journal. Only
                # reported charges count toward the actual-spend limit.
                if (
                    # This stop measures reported charges, not reservations.
                    # Recoverable refusals retain unknown invoices and holds in
                    # the outer ledger; they must not become invented charges.
                    sum(float(record.cost_usd) for record in settled_calls
                        if record.cost_usd is not None)
                    >= self._settled_cost_stop_usd
                ):
                    return "settled_cost_stop"
            self._request_count += 1
            return "accepted"

    def _reserve_payload(self, *, request_bytes: int, output_tokens: int) -> bool:
        with self._lock:
            if (
                self._closed
                or self._request_bytes + request_bytes
                > self._maximum_total_request_bytes
                or self._output_tokens + self._reserved_output_tokens + output_tokens
                > self._maximum_total_output_tokens
                or self._response_bytes
                + self._reserved_response_bytes
                + self._maximum_response_bytes
                > self._maximum_total_response_bytes
            ):
                return False
            self._request_bytes += request_bytes
            self._reserved_output_tokens += output_tokens
            self._reserved_response_bytes += self._maximum_response_bytes
            return True

    @staticmethod
    def _reported_usage(
        body: bytes, ceiling: int
    ) -> tuple[int, int, int, float | None]:
        """Read bounded usage from a provider reply, conservatively on error.

        An unreadable or absent usage block must never look cheaper than the
        reservation it settles, otherwise a malformed reply could mint budget.
        """

        try:
            usage = json.loads(body).get("usage")
        except (UnicodeDecodeError, ValueError, RecursionError, AttributeError):
            return ceiling, 0, 0, None
        if not isinstance(usage, dict):
            return ceiling, 0, 0, None

        def token(name: str, *, maximum: int | None = None) -> int | None:
            raw = usage.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                return None
            return raw if maximum is None else min(raw, maximum)

        output = token("completion_tokens", maximum=ceiling)
        prompt = token("prompt_tokens")
        details = usage.get("prompt_tokens_details", {})
        cache = (
            token("cached_tokens")
            if not isinstance(details, dict)
            else details.get("cached_tokens")
        )
        if isinstance(cache, bool) or not isinstance(cache, int) or cache < 0:
            cache = 0
        raw_cost = usage.get("cost")
        cost = (
            float(raw_cost)
            if not isinstance(raw_cost, bool)
            and isinstance(raw_cost, (int, float))
            and math.isfinite(float(raw_cost))
            and float(raw_cost) >= 0
            else None
        )
        return output if output is not None else ceiling, prompt or 0, cache, cost

    @staticmethod
    def _reported_call_usage(
        body: bytes,
    ) -> tuple[int | None, int | None, int | None, float | None]:
        """Read provider facts without substituting operational reservations."""

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None, None, None, None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return None, None, None, None

        def token(name: str) -> int | None:
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        input_tokens = token("prompt_tokens")
        output_tokens = token("completion_tokens")
        details = usage.get("prompt_tokens_details")
        cache_tokens = (
            details.get("cached_tokens") if isinstance(details, dict) else None
        )
        if (
            isinstance(cache_tokens, bool)
            or not isinstance(cache_tokens, int)
            or cache_tokens < 0
        ):
            cache_tokens = None
        raw_cost = usage.get("cost")
        cost = (
            float(raw_cost)
            if not isinstance(raw_cost, bool)
            and isinstance(raw_cost, (int, float))
            and math.isfinite(float(raw_cost))
            and float(raw_cost) >= 0.0
            else None
        )
        return input_tokens, cache_tokens, output_tokens, cost

    @staticmethod
    def _provider_identity(value: object) -> str | None:
        if (
            isinstance(value, str)
            and len(value) <= 160
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is not None
        ):
            return value
        return None

    @classmethod
    def _response_identity(
        cls, body: bytes, headers: Any
    ) -> tuple[str | None, str | None]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            payload = None
        response_id = cls._provider_identity(
            payload.get("id") if isinstance(payload, dict) else None
        )
        generation_id = None
        for name in ("X-Generation-Id", "X-OpenRouter-Generation-Id"):
            generation_id = cls._provider_identity(headers.get(name))
            if generation_id is not None:
                break
        return response_id, generation_id

    def _record_call_response(
        self,
        request_id: int,
        *,
        body: bytes,
        headers: Any,
        status: str,
        http_status: int | None = None,
    ) -> None:
        input_tokens, cache_tokens, output_tokens, cost_usd = self._reported_call_usage(
            body
        )
        response_id, generation_id = self._response_identity(body, headers)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            payload = None
        choice = None
        reasoning_tokens = None
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
            usage = payload.get("usage")
            details = (
                usage.get("completion_tokens_details")
                if isinstance(usage, dict)
                else None
            )
            if isinstance(details, dict):
                value = details.get("reasoning_tokens")
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    reasoning_tokens = value
        finish_reason = (
            self._provider_identity(choice.get("finish_reason")) if choice else None
        )
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        if type(error_code) is not int or not 400 <= error_code <= 599:
            error_code = None
        # Even fields shaped like identifiers can echo a supplied credential.
        secrets_in_scope = tuple(
            value for value in (self._provider_bearer, self._ephemeral_bearer) if value
        )
        if response_id and any(secret in response_id for secret in secrets_in_scope):
            response_id = None
        if generation_id and any(
            secret in generation_id for secret in secrets_in_scope
        ):
            generation_id = None
        if finish_reason and any(
            secret in finish_reason for secret in secrets_in_scope
        ):
            finish_reason = None
        with self._lock:
            record = self._provider_calls[request_id]
            self._active_upstream_requests[request_id].phase = status
            record.retryable_refusal = service_refusal(http_status, body)
            provider_name = payload.get('provider') if isinstance(payload, dict) else None
            if not provider_name and isinstance(error, dict):
                provider_name = (error.get('metadata') or {}).get('provider_name') if isinstance(error.get('metadata'), dict) else None
            if (isinstance(provider_name, str) and len(provider_name) <= 128
                    and not _CONTROL.search(provider_name)
                    and not any(secret in provider_name for secret in secrets_in_scope)):
                record.provider_name = provider_name
                if 200 <= http_status < 300 and not record.retryable_refusal:
                    # Prefer the endpoint that just served this conversation.
                    # Otherwise every new solver turn hits the same bad primary.
                    for index, route in enumerate(provider_routes(self._provider_only)):
                        if route.split('/')[0].replace('-', '') == provider_name.lower().replace(' ', '').replace('-', ''):
                            self._route_offset = index
            record.response_id = response_id or record.response_id
            record.generation_id = generation_id or record.generation_id
            record.input_tokens = input_tokens
            record.cache_tokens = cache_tokens
            record.output_tokens = output_tokens
            record.cost_usd = cost_usd
            record.finish_reason = finish_reason
            record.reasoning_tokens = reasoning_tokens
            record.http_status = http_status
            record.provider_error_code = error_code
        if (
            finish_reason in {None, "length", "error", "content_filter"}
            # Providers can send HTTP200 headers before reporting a failure in
            # the JSON body. Retain that evidence even without a normal choice
            # or usage; transport success is not a completed model response.
            or (isinstance(payload, dict) and payload.get("error") is not None)
            or (http_status is not None and http_status >= 400)
            or status == "response_oversized"
            or not isinstance(payload, dict)
        ):
            self._preserve_response(
                request_id, body, complete=status != "response_oversized"
            )

    @staticmethod
    def _request_reasoning(value: object) -> dict[str, object] | None:
        """Only operational settings, never arbitrary request text."""
        if not isinstance(value, dict):
            return None
        result: dict[str, object] = {}
        for name in ("enabled", "exclude"):
            if isinstance(value.get(name), bool):
                result[name] = value[name]
        effort = value.get("effort")
        if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            result["effort"] = effort
        maximum = value.get("max_tokens")
        if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
            result["max_tokens"] = maximum
        if set(value) - set(result):
            result["other_fields_omitted"] = True
        return result

    def _preserve_response(
        self, request_id: int, body: bytes, *, complete: bool
    ) -> None:
        """Keep bounded failed-response evidence outside the candidate mount.

        The artifact contains a sanitized body, no transport headers. It is not
        automatically projected to the proposer. A byte limit is explicit and
        does not turn a partial body into a complete response.
        """
        destination = self._response_artifact_dir
        directory_fd = self._response_artifact_dir_fd
        if destination is None or directory_fd is None:
            return
        # Preserve the response allowance already accepted by the broker. A
        # second512KiB ceiling silently discarded long failed generations.
        maximum = self._maximum_response_bytes
        literals = tuple(
            value for value in (self._provider_bearer, self._ephemeral_bearer) if value
        )
        # Redact before truncating so a boundary never exposes a secret prefix.
        cleaned = body
        for secret in literals:
            cleaned = cleaned.replace(secret.encode("utf-8"), b"[REDACTED]")
            escaped = json.dumps(secret, ensure_ascii=True)[1:-1].encode("ascii")
            cleaned = cleaned.replace(escaped, b"[REDACTED]")
        text = cleaned.decode("utf-8", errors="replace")

        structural_omissions = False

        def sanitize(value: object, depth: int = 0) -> object:
            nonlocal structural_omissions
            if depth > 48:
                structural_omissions = True
                return "[NESTED VALUE OMITTED]"
            if isinstance(value, dict):
                return {
                    str(sanitize(str(key), depth + 1)): "[REDACTED]"
                    if str(key).lower().replace("-", "_")
                    in {
                        "authorization",
                        "api_key",
                        "apikey",
                        "access_token",
                        "secret",
                        "password",
                    }
                    else sanitize(item, depth + 1)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [sanitize(item, depth + 1) for item in value]
            if isinstance(value, str):
                for secret in literals:
                    value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, float) and not math.isfinite(value):
                return "[NON-FINITE VALUE]"
            return value

        try:
            original = json.loads(text)
        except (ValueError, RecursionError):
            # A truncated JSON response can still contain complete credential
            # fields. Scrub those fields even though the document cannot parse.
            original = re.sub(
                r'(?i)("(?:authorization|api_key|apikey|access_token|secret|password)"\s*:\s*)"(?:[^"\\]|\\.)*"',
                r'\1"[REDACTED]"',
                text,
            )
        sanitized = sanitize(original)
        sanitized_bytes = _canonical_bytes(sanitized)
        retained_complete = (
            complete and len(sanitized_bytes) <= maximum and not structural_omissions
        )
        body_format = "json" if not isinstance(original, str) else "text"
        if len(sanitized_bytes) > maximum:
            sanitized = sanitized_bytes[:maximum].decode("utf-8", errors="ignore")
            body_format = "truncated_json_text"
        payload = _canonical_bytes(
            {
                "schema": "rsi-arena.provider-response-evidence.v1",
                "ordinal": request_id,
                "body": sanitized,
                "body_format": body_format,
                "body_complete": retained_complete,
                "received_bytes": len(body),
                "retained_sanitized_bytes": min(len(sanitized_bytes), maximum),
                "received_sha256": hashlib.sha256(body).hexdigest(),
                "sanitized": True,
            }
        )
        filename = f"call-{request_id:06d}.json"
        try:
            with self._lock:
                if self._closed or self._response_artifact_dir_fd is None:
                    return
                fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self._response_artifact_dir_fd,
                )
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
        except OSError:
            # Diagnostic persistence must not change the measured action.
            return
        with self._lock:
            record = self._provider_calls[request_id]
            record.response_artifact = f"{destination.name}/{filename}"
            record.response_sha256 = hashlib.sha256(payload).hexdigest()
            record.response_complete = retained_complete

    def _settle_output_token_reservation(
        self,
        reserved: int,
        actual: int,
        *,
        input_tokens: int = 0,
        cache_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        """Charge real output tokens instead of the per-request ceiling.

        Reserving the ceiling forever capped a cell at
        maximum_total_output_tokens / maximum_output_tokens calls even when each
        reply was a fraction of its bound, which failed the longest agent loops.
        """

        with self._lock:
            self._reserved_output_tokens -= reserved
            self._output_tokens += actual
            self._input_tokens += input_tokens
            self._cache_tokens += cache_tokens
            if cost_usd is None:
                self._cost_reported_for_all_responses = False
            else:
                self._cost_usd += cost_usd
            if (
                self._reserved_output_tokens < 0
                or self._output_tokens + self._reserved_output_tokens
                > self._maximum_total_output_tokens
            ):
                raise RuntimeError("provider output token reservation invariant failed")

    def _settle_response_reservation(self, actual_bytes: int | None) -> None:
        with self._lock:
            self._reserved_response_bytes -= self._maximum_response_bytes
            if actual_bytes is not None:
                self._response_bytes += actual_bytes
            if (
                self._reserved_response_bytes < 0
                or self._response_bytes + self._reserved_response_bytes
                > self._maximum_total_response_bytes
            ):
                raise RuntimeError("provider response reservation invariant failed")

    def _begin_upstream_request(
        self, normalized: bytes | None = None, *, explicit_limit: bool = False
    ) -> int:
        now = self._clock()
        with self._lock:
            request_id = self._next_upstream_request_id
            self._next_upstream_request_id += 1
            self._logical_requests_started += 1
            self._upstream_attempts_started += 1
            self._last_progress = now
            self._active_upstream_requests[request_id] = _ActiveUpstreamRequest(
                started_at=now,
                attempt_started_at=now,
            )
            request = json.loads(normalized) if normalized is not None else {}
            self._provider_calls[request_id] = _ProviderCallRecord(
                ordinal=request_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                request_max_tokens=request.get(
                    "max_tokens", request.get("max_completion_tokens")
                ),
                request_max_tokens_source=("explicit" if explicit_limit else "default")
                if normalized is not None
                else None,
                request_reasoning=self._request_reasoning(request.get("reasoning")),
            )
            if self._response_artifact_dir_fd is not None:
                self._journal_fds[request_id] = os.dup(self._response_artifact_dir_fd)
            self._journal_call(request_id, 'dispatch')
            return request_id

    def _journal_call(self, request_id: int, stage: str) -> None:
        """Durable, content-free receipt stages in host-only storage."""
        descriptor = self._journal_fds.get(request_id)
        if descriptor is None:
            return
        payload = _canonical_bytes({'schema':'rsi-arena.provider-call-journal.v1',
            'model_name':'openrouter/'+self._allowed_model,
            'call':self._provider_calls[request_id].snapshot().as_dict()})
        name = f'call-{request_id:06d}-{stage}.json'
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os,'O_NOFOLLOW',0), 0o600, dir_fd=descriptor)
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)

    def _record_response_headers(self, request_id: int, response: Any) -> None:
        """Record generation ID before blocking on the response body."""
        _, generation = self._response_identity(b'', response.headers)
        if generation and any(secret in generation for secret in (self._provider_bearer,self._ephemeral_bearer) if secret):
            generation = None
        with self._lock:
            record=self._provider_calls[request_id]
            record.generation_id=generation
            record.http_status=response.status
            self._journal_call(request_id,'headers')

    def quiesce(self, grace_seconds: float = 5.) -> None:
        """Stop new dispatch; allow existing receipts a bounded time to arrive."""
        with self._lock:
            self._quiescing = True
        deadline = time.monotonic()+grace_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active_upstream_requests:
                    return
            time.sleep(.02)

    def _begin_retry(self, request_id: int) -> None:
        now = self._clock()
        with self._lock:
            active = self._active_upstream_requests[request_id]
            active.attempt += 1
            active.attempt_started_at = now
            active.phase = "opening"
            self._safe_retries += 1
            self._upstream_attempts_started += 1
            self._last_progress = now

    def _record_transport_failure(
        self,
        request_id: int,
        *,
        terminal: bool,
        attempt_was_opening: bool,
    ) -> None:
        now = self._clock()
        with self._lock:
            active = self._active_upstream_requests[request_id]
            active.phase = "transport_failure"
            self._transport_failures += 1
            if terminal:
                self._terminal_transport_failures += 1
                self._terminal_transport_intervals.append(
                    (active.attempt_started_at, now)
                )
                self._last_terminal_transport_failure = now
                self._cost_reported_for_all_responses = False
            if attempt_was_opening:
                self._upstream_attempts_finished += 1
            self._last_transport_failure = now
            self._last_progress = now

    def _record_retry_backoff(self, request_id: int) -> None:
        with self._lock:
            self._active_upstream_requests[request_id].phase = "retry_backoff"

    def _record_response_started(self, request_id: int) -> None:
        now = self._clock()
        with self._lock:
            active = self._active_upstream_requests[request_id]
            first_byte = max(0.0, now - active.attempt_started_at)
            active.phase = "reading_response"
            self._upstream_attempts_finished += 1
            self._responses_started += 1
            self._first_byte_samples += 1
            self._first_byte_total_seconds += first_byte
            self._first_byte_max_seconds = max(
                first_byte,
                self._first_byte_max_seconds or 0.0,
            )
            self._last_progress = now

    def _record_response_finished(self, request_id: int) -> None:
        with self._lock:
            self._active_upstream_requests[request_id].phase = "response_received"
            self._last_progress = self._clock()

    def _upstream_request_phase(self, request_id: int) -> str | None:
        with self._lock:
            active = self._active_upstream_requests.get(request_id)
            return None if active is None else active.phase

    def _finish_upstream_request(self, request_id: int) -> None:
        now = self._clock()
        with self._lock:
            active = self._active_upstream_requests.pop(request_id, None)
            if active is None:
                raise RuntimeError("provider request lifecycle invariant failed")
            record = self._provider_calls[request_id]
            record.elapsed_seconds = round(max(0.0, now - active.started_at), 6)
            if active.phase in {"completed", "response_oversized"}:
                record.status = active.phase
                self._responses_finished += 1
            elif active.phase == "transport_failure":
                record.status = "transport_failed"
            else:
                # A rejected HTTP redirect or an interrupted response handler
                # has finished locally; it must not masquerade as in flight.
                record.status = "response_rejected"
            self._logical_requests_finished += 1
            self._total_upstream_wait_seconds += max(0.0, now - active.started_at)
            self._last_progress = now

            self._journal_call(request_id,'finished')
            descriptor = self._journal_fds.pop(request_id,None)
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _safe_to_retry_transport(exc: BaseException) -> bool:
        """Retry only failures proving no upstream connection was established.

        Chat completions are not known to be idempotent.  A read timeout,
        reset, broken pipe, or generic ``OSError`` may therefore represent a
        request that OpenRouter already accepted and billed; repeating it could
        duplicate work and accounting.  DNS failure and connection refusal
        happen before submission and are the only automatic retry cases.
        """

        reason: object = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return isinstance(reason, (ConnectionRefusedError, socket.gaierror))

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        response_reserved = False
        actual_response_bytes: int | None = None
        reserved_output_tokens = 0
        actual_output_tokens: int | None = None
        actual_input_tokens = 0
        actual_cache_tokens = 0
        actual_cost_usd: float | None = None
        try:
            if not self._authorized(handler):
                self._record_local_rejection("authorization")
                self._reply(handler, 403, b'{"error":"forbidden"}')
                return
            parsed_path = urllib.parse.urlsplit(handler.path)
            if parsed_path.path != _PROVIDER_CHAT_PATH or parsed_path.query:
                self._record_local_rejection("route")
                self._reply(handler, 403, b'{"error":"forbidden"}')
                return
            if handler.headers.get("Transfer-Encoding") is not None:
                self._record_local_rejection("transfer_encoding")
                self._reply(handler, 400, b'{"error":"chunked requests are disabled"}')
                return
            if handler.headers.get_content_type() != "application/json":
                self._record_local_rejection("content_type")
                self._reply(handler, 415, b'{"error":"content type must be JSON"}')
                return
            content_lengths = handler.headers.get_all("Content-Length", failobj=[])
            if len(content_lengths) != 1:
                self._record_local_rejection("content_length")
                self._reply(
                    handler, 400, b'{"error":"exactly one content length is required"}'
                )
                return
            try:
                size = int(content_lengths[0])
            except ValueError:
                size = -1
            if size < 0 or size > self._maximum_request_bytes:
                self._record_local_rejection("request_size")
                self._reply(handler, 413, b'{"error":"request bound exceeded"}')
                return
            claim = self._claim_request()
            if claim != "accepted":
                self._record_local_rejection(claim)
                # The relay may still be sending a large body. Closing before
                # draining it can reset the socket and hide our rejection from
                # the SDK, turning an owned allowance stop into an APIError.
                # Authorization, size and socket-read bounds are checked above.
                try:
                    body = handler.rfile.read(size)
                except (OSError, TimeoutError):
                    self._reply(handler, 408, b'{"error":"request body timed out"}')
                    return
                if len(body) != size:
                    self._reply(handler, 400, b'{"error":"incomplete request"}')
                    return
                self._reply(
                    handler,
                    429,
                    json.dumps(
                        {
                            "error": (
                                SETTLED_COST_STOP_REACHED
                                if claim == "settled_cost_stop"
                                else BUDGET_EXHAUSTED_REQUESTS
                            )
                        }
                    ).encode("ascii"),
                )
                return
            try:
                body = handler.rfile.read(size)
            except (OSError, TimeoutError):
                self._record_local_rejection("request_body_timeout")
                self._reply(handler, 408, b'{"error":"request body timed out"}')
                return
            if len(body) != size:
                self._record_local_rejection("incomplete_body")
                self._reply(handler, 400, b'{"error":"incomplete request"}')
                return
            try:
                normalized, requested_tokens = self._validated_request(body)
            except self.error_type as exc:
                reason = getattr(exc, "broker_rejection_reason", "request_validation")
                fields = getattr(exc, "broker_rejected_fields", ())
                self._record_local_rejection(reason, fields=fields)
                self._reply(handler, 400, b'{"error":"request validation failed"}')
                return
            if not self._reserve_payload(
                request_bytes=len(normalized), output_tokens=requested_tokens
            ):
                self._record_local_rejection("payload_budget")
                self._reply(
                    handler,
                    429,
                    json.dumps({"error": BUDGET_EXHAUSTED_PAYLOAD}).encode("ascii"),
                )
                return
            response_reserved = True
            reserved_output_tokens = requested_tokens
            provider_bearer = self._provider_bearer
            if provider_bearer is None:
                self._reply(handler, 403, b'{"error":"forbidden"}')
                return
            service_attempt = 0
            service_route_start = self._route_offset
            service_started = self._clock()
            request_group = None
            while True:
                request = urllib.request.Request(
                    self._upstream_url,
                    data=normalized,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {provider_bearer}",
                        "Content-Type": "application/json",
                        "User-Agent": "rsi-arena-provider-broker",
                    },
                    method="POST",
                )
                upstream_request_id = self._begin_upstream_request(
                    normalized,
                    explicit_limit=any(
                        name in json.loads(body)
                        for name in ("max_tokens", "max_completion_tokens")
                    ),
                )
                if request_group is None:
                    request_group = upstream_request_id
                with self._lock:
                    self._provider_calls[upstream_request_id].request_group = request_group
                    self._provider_calls[upstream_request_id].configured_route = self._provider_only
                upstream_request_finished = False

                def finish_upstream_request() -> None:
                    nonlocal upstream_request_finished
                    if not upstream_request_finished:
                        self._finish_upstream_request(upstream_request_id)
                        upstream_request_finished = True

                try:
                    try:
                        response = self._open_upstream(
                            request, request_id=upstream_request_id
                        )
                    except urllib.error.HTTPError as exc:
                        if 300 <= exc.code < 400:
                            with self._lock:
                                self._provider_calls[
                                    upstream_request_id
                                ].http_status = exc.code
                            exc.close()
                            finish_upstream_request()
                            self._reply(
                                handler, 502, b'{"error":"upstream redirect rejected"}'
                            )
                            return
                        response = exc
                    with response:
                        upstream_body = response.read(self._maximum_response_bytes + 1)
                        self._record_response_finished(upstream_request_id)
                        actual_response_bytes = min(
                            len(upstream_body), self._maximum_response_bytes
                        )
                        if len(upstream_body) > self._maximum_response_bytes:
                            self._record_call_response(
                                upstream_request_id,
                                body=upstream_body,
                                headers=response.headers,
                                status="response_oversized",
                                http_status=response.status,
                            )
                            finish_upstream_request()
                            self._reply(
                                handler, 502, b'{"error":"response bound exceeded"}'
                            )
                            return
                        (
                            reported_output_tokens,
                            actual_input_tokens,
                            actual_cache_tokens,
                            actual_cost_usd,
                        ) = self._reported_usage(upstream_body, reserved_output_tokens)
                        actual_output_tokens = reported_output_tokens
                        self._record_call_response(
                            upstream_request_id,
                            body=upstream_body,
                            headers=response.headers,
                            status="completed",
                            http_status=response.status,
                        )
                        if provider_bearer.encode("utf-8") in upstream_body:
                            finish_upstream_request()
                            self._reply(
                                handler,
                                502,
                                b'{"error":"response crossed credential boundary"}',
                            )
                            return
                        content_type = response.headers.get_content_type()
                        if content_type not in {"application/json", "text/plain"}:
                            content_type = "application/octet-stream"
                        # Release the reservations before the caller can observe the
                        # reply.  Settling afterwards lets the caller's next request
                        # race this release and be refused while the budget is free.
                        self._settle_response_reservation(actual_response_bytes)
                        self._settle_output_token_reservation(
                            reserved_output_tokens,
                            reported_output_tokens,
                            input_tokens=actual_input_tokens,
                            cache_tokens=actual_cache_tokens,
                            cost_usd=actual_cost_usd,
                        )
                        response_reserved = False
                        finish_upstream_request()
                        refused = service_refusal(response.status, upstream_body)
                        if refused and self._service_retries:
                            delay = service_retry_delay(response.headers, service_attempt + 1, random_source=self._random)
                            elapsed = self._clock() - service_started
                            can_retry = (service_attempt < self._service_retries and
                                         elapsed + delay <= self._service_retry_window and
                                         not self._closed and not self._quiescing)
                            if can_retry:
                                self._sleeper(delay)
                                if self._claim_request() == "accepted":
                                    service_attempt += 1
                                    routed = json.loads(normalized)
                                    route = provider_route_body(self._provider_only, service_route_start + service_attempt, self._allowed_model)
                                    if route:
                                        routed['provider'] = route
                                    normalized = _canonical_bytes(routed)
                                    if self._reserve_payload(request_bytes=len(normalized), output_tokens=requested_tokens):
                                        response_reserved = True
                                        actual_response_bytes = actual_output_tokens = actual_cost_usd = None
                                        actual_input_tokens = actual_cache_tokens = 0
                                        continue
                            with self._lock:
                                self._service_exhausted = True
                        self._reply(
                            handler,
                            response.status,
                            upstream_body,
                            content_type=content_type,
                        )
                        return
                except (
                    OSError,
                    TimeoutError,
                    urllib.error.URLError,
                    IncompleteRead,
                ) as exc:
                    if isinstance(exc, IncompleteRead):
                        self._preserve_response(
                            upstream_request_id, exc.partial, complete=False
                        )
                    if (
                        self._upstream_request_phase(upstream_request_id)
                        == "reading_response"
                    ):
                        self._record_transport_failure(
                            upstream_request_id,
                            terminal=True,
                            attempt_was_opening=False,
                        )
                    finish_upstream_request()
                    self._reply(handler, 502, b'{"error":"upstream unavailable"}')
                    return
                finally:
                    finish_upstream_request()
        finally:
            if response_reserved:
                self._settle_response_reservation(actual_response_bytes)
                self._settle_output_token_reservation(
                    reserved_output_tokens,
                    (
                        reserved_output_tokens
                        if actual_output_tokens is None
                        else actual_output_tokens
                    ),
                    input_tokens=actual_input_tokens,
                    cache_tokens=actual_cache_tokens,
                    cost_usd=actual_cost_usd,
                )

    def _open_upstream(
        self, request: urllib.request.Request, *, request_id: int
    ) -> Any:
        """Open one upstream call, absorbing transport refusals within a bound.

        Only a DNS failure or connection refusal is retried.  An HTTP status is
        an answer, while a timeout/reset may follow successful submission; both
        must not be repeated because Chat Completions have no pinned idempotency
        contract here.
        """

        attempt = 0
        while True:
            try:
                response = self._opener.open(request, timeout=self._timeout_seconds)
            except urllib.error.HTTPError as response:
                self._record_response_started(request_id)
                self._record_response_headers(request_id,response)
                raise
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                retryable = (
                    attempt < self._upstream_retries
                    and self._safe_to_retry_transport(exc)
                )
                self._record_transport_failure(
                    request_id,
                    terminal=not retryable,
                    attempt_was_opening=True,
                )
                if not retryable:
                    raise
                attempt += 1
                if self._upstream_retry_backoff:
                    self._record_retry_backoff(request_id)
                    jitter = float(self._random.uniform(0.75, 1.25))
                    self._sleeper(
                        min(30.0, self._upstream_retry_backoff * attempt * jitter)
                    )
                self._begin_retry(request_id)
                continue
            self._record_response_started(request_id)
            self._record_response_headers(request_id,response)
            return response

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active_sockets)
        # Closing a broker revokes its downstream bearer and sockets promptly.
        # It cannot cancel a provider request already accepted upstream and does
        # not claim paid-call cancellation or exactly-once provider semantics.
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self._started:
            self._server.shutdown()
        self._server.server_close()
        if self._started:
            self._thread.join(timeout=1)
        self._ephemeral_bearer = None
        self._provider_bearer = None
        if self._response_artifact_dir_fd is not None:
            os.close(self._response_artifact_dir_fd)
            self._response_artifact_dir_fd = None
        self._socket_directory.cleanup()


__all__ = [
    "BUDGET_EXHAUSTED_MARKERS",
    "BUDGET_EXHAUSTED_PAYLOAD",
    "BUDGET_EXHAUSTED_REQUESTS",
    "PROVIDER_DIAGNOSTICS_NAME",
    "PROVIDER_RELIABILITY_DIAGNOSTICS_SCHEMA",
    "PROVIDER_TRIAL_DIAGNOSTICS_SCHEMA",
    "SETTLED_COST_STOP_REACHED",
    "WORKER_PROVIDER_BASE_URL",
    "WORKER_PROVIDER_HOST",
    "WORKER_PROVIDER_PORT",
    "WORKER_PROVIDER_SOCKET",
    "ProviderAccountingSnapshot",
    "ProviderCallAccounting",
    "ProviderReliabilitySnapshot",
    "ScopedProviderBroker",
    "ScopedProviderBrokerError",
    "UnixProviderRelay",
    "provider_calls_all_service_refused",
    "provider_wait_dominated_deadline",
]
