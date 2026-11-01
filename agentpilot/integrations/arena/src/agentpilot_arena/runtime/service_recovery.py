"""Bounded HTTP refusal recovery with a separately funded expense journal.

The normal completion/worker receipt accounts for the delivered generation.
Every refused attempt is independently retained and charged or held here before
another dispatch. No token estimate or HTTP error is treated as a zero invoice.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from rsi_arena.benchmarks.harbor_runtime.provider_broker import (
    provider_route_body, provider_routes, service_refusal, service_retry_delay,
)
from agentpilot_arena.runtime.research_worker import write_record


class RefusalLedger:
    def __init__(self, store, root, *, allowance_usd=1.):
        self.store, self.root, self.allowance = store, Path(root), allowance_usd

    def record(self, identity, *, status, body, headers, model, route):
        key = 'service-refusal/' + hashlib.sha256(identity.encode()).hexdigest()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        generation = headers.get('X-Generation-Id') or payload.get('id')
        if not isinstance(generation,str) or re.fullmatch(r'gen-[A-Za-z0-9._:-]{1,155}',generation) is None:
            generation = None
        usage = payload.get('usage') or {}
        cost = usage.get('cost') if isinstance(usage, dict) else None
        import math
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            cost = None
        receipt = {'identity': identity, 'model': model, 'route': route, 'http_status': status,
                   'generation_id': generation, 'cost_usd': cost,
                   'response_sha256': hashlib.sha256(body).hexdigest(),
                   'retry_after': headers.get('Retry-After'), 'body_bytes': len(body)}
        self.root.mkdir(parents=True, exist_ok=True)
        write_record(self.root / (key.split('/')[-1]+'.json'), receipt)
        self.store.reserve_external(key, self.allowance, 'Provider refusal; retained independently of task result')
        self.store.put_document(key, receipt)
        if cost is None:
            self.store.mark_external_unknown(key, 'Refused request invoice pending; do not resubmit this expense')
        else:
            self.store.settle_external(key, cost)


class RecoveringOpener:
    """Return only the final response to an existing accounting adapter.

    No new retry starts after the deadline, and no timeout/partial body is
    resubmitted. The underlying opener disables redirects. Without an external
    ledger this adapter is disabled, so failed expenses cannot disappear.
    """
    def __init__(self, opener, *, route, ledger, identity, max_response_bytes=8*1024*1024,
                 sleeper=time.sleep, clock=time.monotonic, retries=3, window=180., cancelled=lambda: False):
        self.opener, self.route, self.ledger, self.identity = opener, route, ledger, identity
        self.maximum, self.sleeper, self.clock = max_response_bytes, sleeper, clock
        self.retries, self.window = retries, window
        self.cancelled = cancelled
        self.lock, self.ordinal = threading.Lock(), 0
        self.route_offset = 0
        self.exhausted = False
        provider_routes(route)

    def open(self, request, *, timeout):
        with self.lock:
            if self.exhausted:
                raise OSError('Provider recovery exhausted; no further upstream dispatch')
            self.ordinal += 1
            ordinal = self.ordinal
            route_start = self.route_offset
        original = json.loads(request.data)
        started = self.clock()
        for attempt in range(self.retries + 1):
            if self.cancelled():
                raise OSError('Provider recovery stopped before dispatch')
            payload = dict(original)
            route = provider_route_body(self.route, route_start + attempt, original['model'])
            if route:
                payload['provider'] = route
            current = urllib.request.Request(request.full_url, data=json.dumps(payload).encode(),
                headers=dict(request.header_items()), method=request.get_method())
            try:
                response = self.opener.open(current, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            # Successful Responses streams must reach their existing parser.
            # Inspect complete JSON errors only; never buffer an SSE stream.
            content_type = response.headers.get_content_type()
            if content_type == 'text/event-stream':
                if 200 <= response.status < 300:
                    with self.lock:
                        self.route_offset = route_start + attempt
                return response
            with response:
                body = response.read(self.maximum + 1)
            status = response.status
            if len(body) <= self.maximum and service_refusal(status,body):
                delay = service_retry_delay(response.headers,attempt+1)
                if attempt < self.retries and self.clock()-started+delay <= self.window:
                    self.ledger.record(f'{self.identity}/{ordinal}/{attempt}', status=status,
                        body=body, headers=response.headers, model=payload['model'], route=route)
                    self.sleeper(delay)
                    continue
                with self.lock:
                    self.exhausted = True
            if 200 <= status < 300 and not service_refusal(status,body):
                with self.lock:
                    self.route_offset = route_start + attempt
                    try:
                        name = json.loads(body).get('provider','').lower().replace(' ','').replace('-','')
                    except (ValueError, AttributeError):
                        name = ''
                    for index, endpoint in enumerate(provider_routes(self.route)):
                        if endpoint.split('/')[0].replace('-','') == name:
                            self.route_offset = index
            result = io.BytesIO(body)
            result.status, result.headers = status, response.headers
            return result
