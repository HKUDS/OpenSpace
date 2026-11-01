"""Metered model calls available to an isolated coding experiment.

The worker receives a temporary local capability, never a provider credential.
Request identities, native messages, receipts and costs survive the worker.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import threading

from agentpilot.app.provider_calls import ProviderCallContext
from agentpilot.domain import canonical_json, stable_id
from agentpilot.providers import CompletionRequest
from agentpilot.providers.base import AccountingStatus, ProviderCallRefused, ProviderCompletionUnusable
from agentpilot.research.presentation import receipt_view, experiment_result_view
from .research_worker import write_record


@dataclass(frozen=True)
class ExperimentModelRoute:
    completion: object
    model: str
    reservation_usd: float
    max_output_tokens: int = 8192
    temperature: float = .3
    disable_reasoning: bool = False
    reasoning_effort: str | None = None


class ExperimentCallRecoveryRequired(RuntimeError):
    pass


class ExperimentModelServices:
    def __init__(self, *, store, root: Path, routes, max_calls: int = 8, development=None):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError('max_calls must be positive')
        self.store, self.root, self.routes = store, Path(root), dict(routes)
        self.max_calls = max_calls
        self.development = development
        self.root.mkdir(parents=True, exist_ok=True)

    def results(self, ticket_id):
        folder = self.root / stable_id('experiment-services', self.store.namespace, ticket_id)
        index_path = folder / 'calls.json'
        if not index_path.exists():
            return []
        rows = []
        for name, call_id in json.loads(index_path.read_text()).items():
            saved = self.store.load_document('experiment-call-result/' + call_id)
            rows.append(saved if saved is not None else {
                'request_id': name, 'call_id': call_id, 'status': 'unresolved',
                'note': 'No experiment receipt is available; do not infer an observed response.'})
        return rows

    def development_results(self, ticket_id):
        return self.development.results(ticket_id) if self.development is not None else []

    def _account(self, call_id, receipt):
        if receipt.get('accounting_status') == AccountingStatus.EXTERNAL.value:
            self.store.mark_external_billed(call_id)
        else:
            self.store.settle_external(call_id, receipt['cost_usd'])

    def call(self, *, ticket_id, base, request):
        if not isinstance(request, dict) or set(request) - {'request_id', 'messages', 'model'}:
            raise ValueError('call_model accepts request_id, messages and a configured model role')
        name = request.get('request_id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise ValueError('request_id must be a short stable name using letters, digits, _ or -')
        role = request.get('model', 'proposer')
        if role not in self.routes:
            raise ValueError('model must name an available configured role')
        messages = request.get('messages')
        if (not isinstance(messages, list) or not messages or len(messages) > 128
                or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                       or m['role'] not in {'system', 'developer', 'user', 'assistant'}
                       or not isinstance(m['content'], str) for m in messages)
                or len(canonical_json(messages).encode()) > 256 * 1024):
            raise ValueError('messages must be bounded role/content text messages')
        route = self.routes[role]
        call_id = stable_id('research-experiment-call', self.store.namespace, ticket_id, name)
        payload = {'messages': messages, 'model': route.model, 'role': role,
                   'max_output_tokens': route.max_output_tokens, 'temperature': route.temperature,
                   'disable_reasoning': route.disable_reasoning, 'reasoning_effort': route.reasoning_effort,
                   'base_commit': base['commit_sha'], 'reservation_usd': route.reservation_usd}
        completion_request = CompletionRequest(
            model=route.model, prompt='', messages=tuple(messages),
            max_output_tokens=route.max_output_tokens, temperature=route.temperature,
            disable_reasoning=route.disable_reasoning, reasoning_effort=route.reasoning_effort,
            metadata={'purpose': 'worker-research-experiment', 'ticket_id': ticket_id,
                      'request_id': name, 'base_commit': base['commit_sha']})
        folder = self.root / stable_id('experiment-services', self.store.namespace, ticket_id)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.store.put_document('experiment-call-input/' + call_id, payload)
            index_path = folder / 'calls.json'
            index = json.loads(index_path.read_text()) if index_path.exists() else {}
            if name not in index:
                if len(index) >= self.max_calls:
                    raise ValueError('This experiment has reached its model call allowance')
            receipt_key = 'experiment-call-result/' + call_id
            saved = self.store.load_document(receipt_key)
            if saved is not None:
                self._account(call_id, saved)
                return saved
            self.store.reserve_external(call_id, route.reservation_usd,
                                        label='experiment ' + ticket_id + ': ' + name)
            if name not in index:
                index[name] = call_id
                write_record(index_path, index)
            started = folder / (name + '.started.json')
            try:
                if started.exists():
                    recover = getattr(route.completion, 'recover', None)
                    result = recover(call_id) if callable(recover) else None
                    if result is None:
                        raise ExperimentCallRecoveryRequired('No durable receipt; this call was not repeated')
                else:
                    write_record(started, {'call_id': call_id,
                                          'sha256': hashlib.sha256(canonical_json(payload).encode()).hexdigest()})
                    result = route.completion.complete(completion_request, context=ProviderCallContext(
                        call_id=call_id, run_id='research-' + self.store.namespace,
                        batch_id='experiment-' + ticket_id, purpose='worker-research-experiment'))
                if result.usage.cost_usd is None and result.usage.accounting_status is not AccountingStatus.EXTERNAL:
                    raise ExperimentCallRecoveryRequired('Model call has no settled cost')
                saved = {'request_id': name, 'call_id': call_id, 'model': result.model,
                         'response': result.text, 'cost_usd': result.usage.cost_usd,
                         'accounting_status': result.usage.accounting_status.value,
                         'input_tokens': result.usage.input_tokens, 'output_tokens': result.usage.output_tokens,
                         'provider_request_id': result.usage.provider_request_id,
                         'finish_reason': getattr(result, 'raw_finish_reason', None)}
            except ProviderCallRefused:
                saved = {'request_id': name, 'call_id': call_id, 'model': route.model,
                         'error': 'ProviderCallRefused', 'cost_usd': 0.0}
            except ProviderCompletionUnusable as error:
                if error.usage is None or (error.usage.cost_usd is None and error.usage.accounting_status is not AccountingStatus.EXTERNAL):
                    self.store.mark_external_unknown(call_id, type(error).__name__)
                    raise ExperimentCallRecoveryRequired('Model call needs accounting reconciliation') from error
                saved = {'request_id': name, 'call_id': call_id, 'model': route.model,
                         'error': type(error).__name__, 'response': error.settled_text,
                         'accounting_status': error.usage.accounting_status.value,
                         'cost_usd': error.usage.cost_usd}
            except BaseException as error:
                self.store.mark_external_unknown(call_id, type(error).__name__)
                raise
            self.store.put_document(receipt_key, saved)
            self._account(call_id, saved)
            return saved

    @contextmanager
    def open(self, *, ticket_id, base, workspace, experiment_dir):
        directory = Path(experiment_dir)
        if directory.resolve().parent != Path(workspace).resolve() or directory.is_symlink():
            raise ValueError('Experiment service directory must be inside its worktree')
        bearer = secrets.token_urlsafe(32)
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path not in {'/call', '/measure'} or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + bearer):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 512 * 1024:
                        raise ValueError('Request is outside the experiment input limit')
                    request = json.loads(self.rfile.read(size))
                    if self.path == '/call':
                        result = service.call(ticket_id=ticket_id, base=base, request=request)
                    else:
                        if service.development is None:
                            raise ValueError('Development measurement is not configured')
                        result = service.development.measure(ticket_id=ticket_id, base=base,
                                                             workspace=Path(workspace), request=request)
                    response, status = {'result': receipt_view(result) if self.path == '/call' else experiment_result_view(result)}, 200
                except Exception as error:
                    # Never relay provider exception text or host paths.
                    response, status = {'error': type(error).__name__}, 400
                data = canonical_json(response).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(15)
                return connection, address

        server = Server(('127.0.0.1', 0), Handler)
        address = 'http://127.0.0.1:' + str(server.server_port) + '/call'
        client = directory / 'client.py'
        if client.exists() or client.is_symlink():
            server.server_close()
            raise ValueError('Experiment helper path is already occupied')
        source = '''"""Model calls. Reuse a request_id only to recover the identical call."""
import json
from pathlib import Path
import urllib.request

def call_model(request_id, messages, model='proposer'):
    payload = json.dumps({'request_id': request_id, 'messages': messages, 'model': model}).encode()
    request = urllib.request.Request(ADDRESS, data=payload, headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request) as response:
        result = json.load(response)['result']
    folder = Path(__file__).parent / 'model-results'
    folder.mkdir(exist_ok=True)
    (folder / (request_id + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
'''
        if self.development is not None:
            source += '''
def measure_a(request_id, tasks=None, replicates=1, artifact=None, control=None):
    """Measure current experimental source (or an existing artifact) against its base on A."""
    payload = json.dumps({'request_id': request_id, 'tasks': tasks, 'replicates': replicates,
                          'artifact': artifact, 'control': control}).encode()
    request = urllib.request.Request(ADDRESS.rsplit('/', 1)[0] + '/measure', data=payload,
        headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request) as response:
        return json.load(response)['result']
'''
        client.write_text('ADDRESS = ' + repr(address) + '\nTOKEN = ' + repr(bearer) + '\n' + source)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {'helper': '.agentpilot-experiment/client.py',
                   'call': 'call_model(request_id, messages, model="proposer")',
                   'models': {name: route.model for name, route in self.routes.items()},
                   **({'development': {'call': 'measure_a(request_id, tasks=None, replicates=1, artifact=None, control=None)',
                                      'tasks': list(self.development.adapter.task_ids),
                                      'source': 'Omit artifact to freeze the current edited program. The comparison defaults to this experiment base. This is development evidence, not admission.'}}
                      if self.development is not None else {}),
                   'messages': 'Exact role/content messages; no template rewriting. Reuse an ID only for the identical request.'}
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            client.unlink(missing_ok=True)
