"""Run or resume lightweight portfolio research over an existing evidence packet.

Preview is the default and never dispatches the model or a queued experiment.
The research session is separate from the legacy single-action BatchLoop.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

from agentpilot.app.cli import _load_env_file
from agentpilot.app.runtime_config import load_app_config
from agentpilot.domain import AdapterTraceCapability, canonical_json
from agentpilot.persistence.sqlite import _loads
from agentpilot.policy.proposer.base import StructuredProposalRequest
from agentpilot.policy.proposer.portfolio_session import PortfolioResearchSession
from agentpilot.policy.proposer.trace_agentic import TraceAgenticBudget, TraceAgenticInputs, TraceAgenticProposer
from agentpilot.research import ResearchCoordinator, ResearchStore
from agentpilot.research.evolution import ResearchEvolution
from agentpilot.research.presentation import research_view
from agentpilot.search import TraceStore, VisibleATraceFileSink
from agentpilot_arena.aime.measure_root import build_executor
from agentpilot_arena.aime.settings import parse_aime_settings, visible_a_task_ids
from agentpilot_arena.runtime.investigation_probe import BwrapInvestigationProbe
from agentpilot_arena.runtime.repository_map import ImmutableGitDefinitionReader
from agentpilot_arena.runtime.structural_adherence import ImmutableGitReaderConfig, SubprocessImmutableGitObjectReader
from agentpilot_arena.runtime.research_worker import PortfolioWorkerBuilder, write_record, write_text_record
from agentpilot_arena.runtime.trace_capture import HARBOR_TRACE_CONTRACT


class PreviewCompletion:
    def complete(self, *args, **kwargs):
        raise RuntimeError('Preview does not dispatch model calls')


class ProposerOnlyBackend:
    """Expose normal tool capabilities while forbidding every work dispatch."""
    def __init__(self, backend):
        self.backend = backend

    def capabilities(self):
        return self.backend.capabilities()

    def prepare(self, *args, **kwargs):
        raise RuntimeError('Proposer-only mode cannot dispatch workers or measurements')

    execute = prepare
    recover = prepare


class LazyCellExecutor:
    """Keep preview/setup independent of physical solver startup."""
    def __init__(self, factory, *, settled_cost_stop_usd=None):
        self.factory, self.executor = factory, None
        self.settled_cost_stop_usd = settled_cost_stop_usd
        self.lock = threading.Lock()

    def measure(self, *, program_id, cell):
        with self.lock:
            if self.executor is None:
                self.executor = self.factory()
        return self.executor.measure(program_id=program_id, cell=cell)


class PreviewStore:
    """Session rendering must not replace an actual research checkpoint."""
    def __init__(self, store, *, inherit=False):
        self.store, self.checkpoints, self.documents = store, {}, {}
        self.inherit = inherit

    def __getattr__(self, name):
        return getattr(self.store, name)

    def load_checkpoint(self, key, default=None):
        return self.checkpoints[key] if key in self.checkpoints else (
            self.store.load_checkpoint(key, default) if self.inherit else default)

    def checkpoint(self, key, value):
        self.checkpoints[key] = value

    def put_document(self, key, value):
        if self.inherit:
            previous = self.store.load_document(key)
            if previous is not None and previous != value:
                raise ValueError('Preview differs from the frozen research strategy')
        if key in self.documents and self.documents[key] != value:
            raise ValueError('Preview document changed')
        self.documents[key] = value
        return key

    def load_document(self, key, default=None):
        return self.documents[key] if key in self.documents else (
            self.store.load_document(key, default) if self.inherit else default)


def select_session(store, *, requested=None, resume=False, execute=True):
    """Select exactly one old conversation or one explicitly named new one."""

    previous = store.load_checkpoint("driver-session", default=None)
    if not execute:
        key = requested or (previous or {}).get("key") or "session-1"
        return {"key": key, "ordinal": (previous or {}).get("ordinal", 1), "new": previous is None}
    if previous is None:
        if resume:
            raise ValueError("No research conversation exists to resume")
        return {"key": requested or "session-1", "ordinal": 1, "new": True}
    prior_key = previous["key"]
    prior_ordinal = int(previous.get("ordinal", 1))
    if resume:
        if requested is not None and requested != prior_key:
            raise ValueError(
                f"--resume restores {prior_key!r}; omit --resume to start the new --session"
            )
        if store.load_checkpoint("portfolio-session/" + prior_key, default=None) is None:
            raise ValueError("The recorded research conversation has no checkpoint")
        return {"key": prior_key, "ordinal": prior_ordinal, "new": False}
    if requested is None:
        raise ValueError(
            "Portfolio exists; use --resume for the current conversation or --session NEW for another"
        )
    if requested == prior_key or store.load_checkpoint(
        "portfolio-session/" + requested, default=None
    ) is not None:
        raise ValueError("Existing research conversations require --resume")
    # An explicit new key also permits moving past an older prompt contract.
    # Preserve the old checkpoint and its frozen jobs; never reinterpret its
    # pending model response as a response to the new research instructions.
    return {"key": requested, "ordinal": prior_ordinal + 1, "new": True}


class RecordedOpenRouterCompletion:
    """Existing durable accounting, with actual wire/response artifacts."""

    def __init__(self, *, root: Path, key: str, provider_only: str | None = None,
                 timeout=1200, omit_temperature=False, json_object_mode=False, refusal_ledger=None,
                 retry_sleeper=time.sleep):
        from openai import OpenAI
        from agentpilot.app.provider_calls import DurableCompletion
        from agentpilot.persistence import SQLiteUnitOfWorkFactory
        from agentpilot.providers import (
            OpenRouterAdapter, GenerationAccountingReconciler, OpenRouterGenerationLookup,
        )
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()
        self.route_offset = 0
        self.route_lock = threading.Lock()
        from rsi_arena.benchmarks.harbor_runtime.provider_broker import provider_routes, provider_route_body, service_refusal, service_retry_delay
        routes = provider_routes(provider_only)
        client = OpenAI(api_key=key, base_url='https://openrouter.ai/api/v1', timeout=timeout, max_retries=0)

        def create(**payload):
            call = self.local.call_id
            if omit_temperature:
                payload.pop('temperature', None)
            if json_object_mode and (payload.get('response_format') or {}).get('type')=='json_schema':
                # Memory validates its full schema locally. JSON mode permits
                # endpoints that do not implement provider-side schema decoding.
                payload['response_format']={'type':'json_object'}
            extra = dict(payload.get('extra_body') or {})
            extra['provider'] = {**extra.get('provider',{}), **provider_route_body(provider_only, model=payload['model'])}
            payload['extra_body'] = extra
            write_record(root / (call + '.wire.json'), payload)
            started = time.monotonic()
            with self.route_lock:
                route_start = self.route_offset
            retries = 3 if routes and refusal_ledger is not None else 0
            for attempt in range(retries+1):
                if provider_only:
                    payload['extra_body']['provider'] = provider_route_body(provider_only,route_start+attempt, payload['model'])
                write_record(root / f'{call}.attempt-{attempt}.wire.json', payload)
                error = None
                try:
                    response = client.chat.completions.create(**payload)
                    raw = response.model_dump(mode='json')
                    status, body, headers = 200, json.dumps(raw).encode(), {}
                except __import__('openai').APIStatusError as exc:
                    error = exc
                    status, body, headers = exc.status_code, exc.response.content, exc.response.headers
                if service_refusal(status,body) and attempt < retries:
                    delay = service_retry_delay(headers,attempt+1)
                    if time.monotonic()-started+delay <= 180.:
                        refusal_ledger.record(f'{root}/{call}/{attempt}',status=status,body=body,headers=headers,
                            model=payload['model'],route=payload.get('extra_body',{}).get('provider'))
                        retry_sleeper(delay)
                        continue
                if error is not None:
                    raise error
                if not service_refusal(status,body):
                    with self.route_lock:
                        self.route_offset = route_start + attempt
                        name = str(raw.get('provider','')).lower().replace(' ','').replace('-','')
                        for index, endpoint in enumerate(routes):
                            if endpoint.split('/')[0].replace('-','') == name:
                                self.route_offset = index
                write_record(root / (call + '.raw-response.json'), raw)
                return response

        transport = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.inner = DurableCompletion(
            provider=OpenRouterAdapter(transport),
            unit_of_work=SQLiteUnitOfWorkFactory(root / 'provider.sqlite3'),
            require_provider_reported_cost=True,
            allow_pending_accounting=True,
            accounting_reconciler=GenerationAccountingReconciler(
                OpenRouterGenerationLookup(api_key=key), pending_grace_seconds=30,
            ),
        )

    def complete(self, request, *, context):
        self.local.call_id = context.call_id
        write_record(self.root / (context.call_id + '.context.json'), json.loads(canonical_json(context)))
        return self.inner.complete(request, context=context)

    def recover(self, call_id):
        return self.inner.recover(call_id)


from agentpilot_arena.runtime.research_sources import ArtifactSourceReader


def _copy_packet(source: Path, destination: Path):
    """Keep historical frozen packets unchanged while adding new A evidence."""
    if not destination.exists():
        shutil.copytree(source, destination)
    for directory, _, _ in os.walk(destination):
        Path(directory).chmod(0o700)
    for name in ('request.json', 'manifest.json'):
        if hashlib.sha256((source / name).read_bytes()).digest() != hashlib.sha256((destination / name).read_bytes()).digest():
            raise ValueError('Research output belongs to a different evidence packet')


def assemble(args, *, proposer_completion=None, solver_completion=None, policy_completion=None, embedding_backend=None):
    from agentpilot_arena.runtime.research_experiment import PortfolioWorkerExperimentRunner
    from agentpilot_arena.runtime.research_experiment_services import ExperimentModelServices, ExperimentModelRoute
    from agentpilot_arena.runtime.research_development_service import ExperimentDevelopmentService, ExperimentSnapshotter
    from agentpilot_arena.runtime.research_component import ProgramTemplateCallAdapter
    from agentpilot_arena.runtime.research_portfolio import (
        ArenaResearchPortfolioBackend, ArenaDevelopmentMeasurementAdapter,
        GitPatchPrototypeMaterializer, register_visible_result,
    )
    from agentpilot_arena.runtime.research_admission import FrozenBAdmissionEvaluator
    from agentpilot_arena.runtime.research_policy import build_research_policy

    if args.execute and args.call_allowance_usd is None:
        raise ValueError('--call-allowance-usd is required for paid execution')
    config = load_app_config(args.config)
    from agentpilot_arena.runtime.research_environment import ResearchEnvironment
    from agentpilot_arena.task_scheduling import TaskPool, PooledExecutor
    from agentpilot_arena.execution_journal import ResilientAdmission
    proposer_only = getattr(args, 'proposer_only', False)
    experiments_only = getattr(args, 'experiments_only', False)
    if proposer_only and experiments_only:
        raise ValueError('Choose proposer-only or experiments-only, not both')
    research_environment = ResearchEnvironment(config, validate=args.execute and not proposer_only,
        **({'development_only': True} if experiments_only else {}))
    settings, runtime = research_environment.settings, research_environment.runtime
    policy = config.policy.memory if config.policy.memory is not None else config.policy.ucb
    model_config = policy.proposer
    model = args.model or model_config.model
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = args.evidence.resolve()
    packet = output / 'evidence-packet'
    _copy_packet(source, packet)
    request = StructuredProposalRequest(**_loads((packet / 'request.json').read_text()))
    manifest = _loads((packet / 'manifest.json').read_text())
    store = ResearchStore(output / 'portfolio.sqlite3', budget_usd=args.budget_usd, budget_mode='actual')
    if args.execute:
        mode = 'proposer_only' if proposer_only else 'experiments_only' if experiments_only else 'research'
        existing_mode = store.load_checkpoint('execution-mode')
        if existing_mode is not None and existing_mode != mode:
            raise ValueError('Execution mode changed; use a separate output directory')
        if proposer_only and store.tickets():
            raise ValueError('Proposer-only mode requires a portfolio without dispatched work')
        if experiments_only:
            from agentpilot_arena.research_observation import validate_experiment_only_store
            validate_experiment_only_store(store)
            measurement_context = research_environment.measurement_context
            prior_context = store.load_checkpoint('development-measurement-context')
            if prior_context is not None and prior_context != measurement_context:
                raise ValueError('Development measurement environment changed; use a new observation output')
            store.checkpoint('development-measurement-context', measurement_context)
        store.checkpoint('execution-mode', mode)
    objects = SubprocessImmutableGitObjectReader(ImmutableGitReaderConfig(
        repository=runtime.program_repository, git_executable=runtime.candidate.git_executable,
        timeout_seconds=runtime.candidate.git_timeout_seconds,
        max_output_bytes=4 * 1024 * 1024, max_blob_bytes=1024 * 1024,
    ))
    objects.resolve_commit(request.parent_selection.commit_sha)
    base_record = store.load_checkpoint('root-artifact', default=None)
    if base_record is None:
        base = store.register_base(request.parent_selection.commit_sha, metadata={
            'program_id': request.parent_selection.parent_id, 'source_request': request.request_id,
        })
        store.checkpoint('root-artifact', {'id': base.id})
    else:
        base = store.artifact(base_record['id'])
        if base.commit_sha != request.parent_selection.commit_sha:
            raise ValueError('Portfolio base differs from the supplied evidence')
    fingerprint = hashlib.sha256(canonical_json({
        'config_sha256': hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),
        'request_id': request.request_id, 'model': model, 'provider_only': args.provider_only,
        'call_allowance_usd': args.call_allowance_usd,
    }).encode()).hexdigest()
    old = store.load_checkpoint('runtime-identity', default=None)
    if old is not None and store.load_document('research-policy/identity') is None:
        raise ValueError('This portfolio predates strategy learning; start a new output directory instead of relabelling its history as UCB/Memory')
    if args.execute and old is not None and old['sha256'] != fingerprint:
        raise ValueError('Frozen runtime or model differs; start a separate portfolio')
    if args.execute:
        store.checkpoint('runtime-identity', {'sha256': fingerprint})
    environment = dict(os.environ)
    if proposer_only:
        solver_completion = solver_completion or PreviewCompletion()
        policy_completion = policy_completion or PreviewCompletion()
    from agentpilot_arena.runtime.service_recovery import RefusalLedger
    refusal_ledger = RefusalLedger(store, output/'service-refusals', allowance_usd=args.call_allowance_usd or 1.)
    supplied_proposer = proposer_completion is not None
    routes, effective = {}, {}

    def policy_route(role, role_config, *, access, endpoint=None):
        from agentpilot_arena.runtime.completion_routes import resolve_completion_route
        from agentpilot_arena.runtime.worker_completion import RecordedWorkerCompletion

        route = resolve_completion_route(role_config, worker=getattr(runtime, 'worker', None),
            providers=settings.provider_runtime, environment=environment,
            fallback_key_env=runtime.program.secret_env_name, access=access)
        if route.access == 'worker' and role == 'proposer':
            if args.model is not None and args.model != route.model:
                raise ValueError('--model cannot override a reused worker model; configure its model/login or use API access')
        if route.access == 'worker' and endpoint:
            raise ValueError('Endpoint allowlists require API access; configure the reused worker provider in worker.toml')
        routes[role], effective[role] = asdict(route), route.configure(role_config)
        root = output / ('proposer-calls' if role == 'proposer' else 'policy-calls')
        if route.access == 'worker':
            return RecordedWorkerCompletion(root=root if role == 'proposer' else root / role,
                settings=runtime.worker, environment=environment,
                max_prompt_bytes=max(runtime.worker.max_prompt_bytes, model_config.max_context_bytes + 65536))
        return RecordedOpenRouterCompletion(root=root, key=environment[route.api_key_env],
            provider_only=endpoint, json_object_mode=role != 'proposer',
            refusal_ledger=refusal_ledger, timeout=runtime.program.provider_timeout_seconds)

    if args.execute:
        if proposer_completion is None:
            proposer_completion = policy_route('proposer', replace(model_config, model=model),
                access=getattr(args, 'proposer_access', 'auto'), endpoint=args.provider_only)
            model_config, model = effective['proposer'], effective['proposer'].model
        if solver_completion is None:
            key_name = runtime.program.secret_env_name
            key = environment.get(key_name)
            if not key:
                raise ValueError(f'Missing task-solver credential {key_name}; worker login covers policy calls, not benchmark evaluation')
            solver_completion = RecordedOpenRouterCompletion(
                root=output / 'component-calls', key=key, provider_only=runtime.program.provider_only,
                refusal_ledger=refusal_ledger, timeout=runtime.program.provider_timeout_seconds,
                omit_temperature=runtime.program.temperature is None)
        if policy_completion is None and not supplied_proposer and config.policy.memory is not None:
            endpoint = getattr(args, 'policy_provider_only', None) or (
                runtime.codex.provider_only if runtime.codex is not None else None)
            policy_completion = {role: policy_route(role, getattr(config.policy.memory, role),
                access=getattr(args, 'policy_access', 'auto'), endpoint=endpoint)
                for role in ('ranker', 'summarizer')}
        if routes:
            previous = store.load_checkpoint('completion-routes')
            if previous is not None and previous != routes:
                raise ValueError('Completion access changed on resume; keep the original API/worker routes or use a new output directory')
            store.checkpoint('completion-routes', routes)
            policy = replace(policy, **effective)
            config = replace(config, policy=replace(config.policy,
                **({'memory': policy} if config.policy.memory is not None else {'ucb': policy})))
    else:
        # A preview of a saved native run uses its frozen role identities without
        # constructing a CLI, checking a login, or reading API credentials.
        from agentpilot_arena.runtime.completion_routes import CompletionRoute
        previous = store.load_checkpoint('completion-routes', {})
        if previous:
            effective = {role: CompletionRoute(**route).configure(getattr(policy, role))
                         for role, route in previous.items()}
            policy = replace(policy, **effective)
            config = replace(config, policy=replace(config.policy,
                **({'memory': policy} if config.policy.memory is not None else {'ucb': policy})))
            model_config, model = policy.proposer, policy.proposer.model
    proposer_completion = proposer_completion or PreviewCompletion()
    solver_completion = solver_completion or PreviewCompletion()
    if policy_completion is None:
        policy_completion = proposer_completion
    trace_store = TraceStore(packet)
    probe = BwrapInvestigationProbe(output / 'python-probes', bwrap_path=runtime.program.bwrap_path)
    definition_reader = ImmutableGitDefinitionReader(
        objects, read_only_roots=runtime.candidate.read_only_roots,
        entrypoint_paths=runtime.candidate.entrypoint_paths,
        entrypoint_symbols=runtime.candidate.entrypoint_symbols,
    )
    inputs = TraceAgenticInputs((manifest,), trace_store, definition_reader, investigation_probe=probe)
    engine = TraceAgenticProposer(
        proposer_completion, model=model, max_output_tokens=model_config.max_output_tokens,
        temperature=model_config.temperature, disable_reasoning=model_config.disable_reasoning,
        reasoning_effort=model_config.reasoning_effort,
        inputs=SimpleNamespace(get=lambda _: inputs), source_context=True, investigation=True,
        research_strategy='interleaved', investigation_scope='all_failures',
        guide_enabled=model_config.guide_enabled, max_context_bytes=model_config.max_context_bytes,
        group_repeated_tasks=model_config.group_repeated_tasks,
        same_task_pass_examples=model_config.same_task_pass_examples,
        budget=TraceAgenticBudget(
            model_config.max_proposer_turns, model_config.max_trace_reads,
            model_config.max_repository_reads, model_config.max_read_bytes,
            max_read_calls=model_config.max_read_calls,
        ),
    )
    worker = PortfolioWorkerBuilder(runtime=runtime, request=request, root=output / 'builds', environment=environment,
                                   refusal_ledger=refusal_ledger)
    from agentpilot_arena.research_reuse import configure_worker
    configure_worker(worker,refusal_ledger=refusal_ledger)
    solver_model = runtime.program.model_name.removeprefix('openrouter/')
    experiment_services = ExperimentModelServices(store=store, root=output / 'experiment-calls', routes={
        'proposer': ExperimentModelRoute(proposer_completion, model, args.call_allowance_usd or 1.,
            max_output_tokens=model_config.max_output_tokens, temperature=model_config.temperature,
            disable_reasoning=model_config.disable_reasoning, reasoning_effort=model_config.reasoning_effort),
        'solver': ExperimentModelRoute(solver_completion, solver_model, args.call_allowance_usd or 1.,
            max_output_tokens=runtime.program.maximum_output_tokens,
            temperature=runtime.program.temperature if runtime.program.temperature is not None else .3,
            disable_reasoning=runtime.program.reasoning_effort == 'none',
            reasoning_effort=runtime.program.reasoning_effort if runtime.program.reasoning_effort not in {'default', 'none'} else None),
    })
    experiment_runner = PortfolioWorkerExperimentRunner(runtime=runtime, request=request,
        root=output / 'experiments', environment=environment, services=experiment_services)
    prototype = GitPatchPrototypeMaterializer(
        workspaces=worker.workspaces, validator=worker.validator,
        action_factory=worker.action_factory.prototype_action, journal_root=output / 'prototypes',
        namespace=store.namespace,
    )
    source_reader = ArtifactSourceReader(objects, store, (*runtime.candidate.editable_roots, *runtime.candidate.read_only_roots),
        request=request, editable_roots=runtime.candidate.editable_roots,
        definition_reader=definition_reader, max_files=runtime.candidate.max_files)
    component = ProgramTemplateCallAdapter(
        solver_completion, store.artifact, source_reader.text,
        model=solver_model, allowed_roots=source_reader.roots,
        reservation_usd=args.call_allowance_usd or 1.0, default_artifact=base.id,
        max_output_tokens=runtime.program.maximum_output_tokens,
        temperature=runtime.program.temperature if runtime.program.temperature is not None else 0.3,
        disable_reasoning=runtime.program.reasoning_effort == 'none',
        reasoning_effort=runtime.program.reasoning_effort if runtime.program.reasoning_effort not in {'default', 'none'} else None,
    )
    concurrency = getattr(args, 'task_concurrency', 1)
    task_pool = TaskPool(getattr(args, 'task_pool', None) or output / 'task-pool', concurrency, owner=str(output.resolve()))
    cell_allowance = getattr(args, 'cell_allowance_usd', None)
    if cell_allowance is None:
        cell_allowance = settings.cell_runtime.settled_cost_stop_usd * 2
    development = ArenaDevelopmentMeasurementAdapter(
        executor=PooledExecutor(LazyCellExecutor(lambda: research_environment.executor(
            trace_sink=VisibleATraceFileSink(trace_store, AdapterTraceCapability(
                adapter_id='portfolio-' + research_environment.benchmark + '-harbor', format='harbor-agent-trajectory',
                supports_complete=True, supports_timeout_partial=True,
                trace_contract=HARBOR_TRACE_CONTRACT))),
            settled_cost_stop_usd=settings.cell_runtime.settled_cost_stop_usd), task_pool),
        repository=runtime.program_repository, journal_root=output / 'development',
        task_ids=research_environment.task_ids,
        estimated_cost_per_cell_usd=cell_allowance,
        concurrency=concurrency,
        namespace=store.namespace,
        trace_artifact_resolver=trace_store.artifact,
    )
    experiment_services.development = ExperimentDevelopmentService(store=store,
        root=output / 'experiment-development', adapter=development,
        snapshotter=ExperimentSnapshotter(experiment_runner))
    admission = FrozenBAdmissionEvaluator(
        runtime=runtime, request=request,
        executor=PooledExecutor(LazyCellExecutor(lambda: research_environment.executor(),
            settled_cost_stop_usd=settings.cell_runtime.settled_cost_stop_usd), task_pool),
        root=output / 'confirmation-private',
        action_factory=worker.action_factory,
        estimated_cost_per_cell_usd=cell_allowance,
        concurrency=concurrency,
    )
    backend = ArenaResearchPortfolioBackend(
        artifact_resolver=store.artifact, default_base_id=base.id, python_probe=probe,
        prototype=prototype, builder=worker, development=development, admission=ResilientAdmission(admission),
        component_call=component,
        experiment_runner=experiment_runner,
        experiment_resolver=store.get_ticket,
    )
    from agentpilot_arena.admission_accounting import AdmissionAccounting, AccountedResearchBackend
    from agentpilot_arena.research_reuse import ReusableBuildBackend
    from agentpilot_arena.admission_recovery import AdmissionRecovery
    recovery = AdmissionRecovery(store, admission, model=runtime.program.model_name,
        hold_usd=max(cell_allowance, settings.cell_runtime.settled_cost_stop_usd or cell_allowance),
        measurement_identity=settings.root_seed.apparatus_digest, pool=task_pool, executor=admission.executor.executor)
    accounted_backend = AccountedResearchBackend(ReusableBuildBackend(backend,store), AdmissionAccounting(store, output / 'confirmation-private',
        model=runtime.program.model_name, hold_usd=max(cell_allowance, settings.cell_runtime.settled_cost_stop_usd or cell_allowance)), recovery)
    execution_backend = ProposerOnlyBackend(accounted_backend) if proposer_only else accounted_backend
    if experiments_only:
        from agentpilot_arena.research_observation import ExperimentsOnlyBackend
        execution_backend = ExperimentsOnlyBackend(accounted_backend,
            measurement_context=research_environment.measurement_context)
    coordinator = ResearchCoordinator(store, execution_backend, max_workers=args.parallel, autostart=False)
    coordinator.acceptance_measurements = 2 * (runtime.sampling.size * runtime.sampling.replicates
        if runtime.sampling is not None else len(runtime.paired_plan.cells))
    try:
        if getattr(args, '_planned_session_key', None) is not None and args.execute:
            selected = ResearchEvolution(store, base.id).planned_session(args._planned_session_key, persist_context=False)
        else:
            selected = select_session(
                store, requested=args.session, resume=args.resume, execute=args.execute,
            )
    except BaseException:
        coordinator.shutdown(wait=False)
        raise
    key, ordinal = selected["key"], selected["ordinal"]
    try:
        strategy = build_research_policy(config, settings, store if args.execute else PreviewStore(store, inherit=True), base, request, worker,
            completion=policy_completion, allowance=args.call_allowance_usd or 1.,
            environment=environment, preview=not args.execute or proposer_only, embedding_backend=embedding_backend)
    except BaseException:
        coordinator.shutdown(wait=False)
        raise
    coordinator.policy = strategy if args.execute else None
    evolution = ResearchEvolution(store, base.id, policy=strategy)
    context = evolution.context(key, persist=args.execute)
    strategy.session_key = key
    working = store.artifact(context['base_artifact_id'])
    backend.default_base_id = working.id
    component.default_artifact = working.id
    if not args.execute:
        coordinator.store = PreviewStore(store)
    from agentpilot_arena.runtime.research_trace_recovery import restore_portfolio_trace_artifacts
    restore_portfolio_trace_artifacts(trace_store, store, output / 'development', persist=args.execute)
    session = PortfolioResearchSession(
        engine, request, coordinator, session_key=key,
        completion_reservation_usd=args.call_allowance_usd or 1.0,
        result_registrar=register_visible_result, artifact_reader=source_reader.read, artifact_navigator=source_reader.navigate,
        working_artifact=working, research_history=context['history'],
        policy_context=context.get('policy_context'),
    )
    session.evolution = evolution
    session.research_policy = strategy
    if args.execute:
        store.checkpoint('driver-session', {'key': key, 'ordinal': ordinal})
    return session, coordinator


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True, help='AIME or Terminal-Bench 2.1 runtime TOML')
    p.add_argument('--evidence', type=Path, required=True, help='Existing packet containing request.json, manifest.json and evidence/')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--model', help='Exact provider model ID; omitted uses configured proposer')
    p.add_argument('--proposer-access', choices=('auto', 'api', 'worker'), default='auto',
                   help='Use proposer API credentials when present, otherwise reuse worker login (default: auto)')
    p.add_argument('--policy-access', choices=('auto', 'api', 'worker'), default='auto',
                   help='Access for Memory ranking/summaries; auto reuses worker login when their API credentials are absent')
    p.add_argument('--provider-only', help='Comma-separated OpenRouter endpoint allowlist, in priority order; one slug pins a provider')
    p.add_argument('--policy-provider-only',help='Optional endpoint for Memory ranking and summaries')
    p.add_argument('--env-file', type=Path)
    p.add_argument('--budget-usd', type=float, help='Shared research budget; omitted records costs without a portfolio ceiling')
    p.add_argument('--call-allowance-usd', type=float, help='Reserved estimate per model call; required with --execute')
    p.add_argument('--parallel', type=int, default=3)
    p.add_argument('--task-concurrency', type=int, default=1, help='Shared concurrent benchmark task limit')
    p.add_argument('--task-pool', type=Path, help='Share this task-pool directory across policies and baseline collection')
    p.add_argument('--cell-allowance-usd', type=float, help='Reservation estimate for one solver task; independent of its cost-stop threshold')
    p.add_argument('--turns', type=int, help='Maximum model calls in this invocation, from config if omitted')
    p.add_argument('--session', help='Conversation key; name a new key to start another research conversation')
    p.add_argument('--rounds', type=int, help='Plan a persisted sequence of research sessions; omitted starts one')
    p.add_argument('--execute', action='store_true', help='Dispatch paid model/worker/measurement work')
    observation = p.add_mutually_exclusive_group()
    observation.add_argument('--proposer-only', action='store_true',
        help='Observe real proposer calls over historical source/A evidence; stop before experiment/build/evaluate. No worker or benchmark execution; current measurement-apparatus validation is unnecessary.')
    observation.add_argument('--experiments-only', action='store_true',
        help='Run local experiments and observe result-driven decisions; stop before build/evaluate. B dispatch is forbidden; A measurements retain their current environment identity.')
    p.add_argument('--feedback-turns', type=int, default=16,
        help='Experiments-only: model calls reserved after the first experiment result arrives')
    p.add_argument('--max-experiments', type=int, default=2,
        help='Experiments-only: maximum new experiments in this observation')
    p.add_argument('--resume', action='store_true', help='Resume only the currently recorded conversation')
    return p


def _requires_proposer(ticket, store):
    """Whether a settled ticket carries a new research decision."""

    if getattr(ticket, "kind", None) == "admit":
        return False
    if getattr(ticket, "kind", None) != "build" or ticket.status != "succeeded":
        return True
    followups = getattr(store, "followups", None)
    if not callable(followups):
        return True
    return not any(
        row.get("kind") == "admit" and row.get("build_ticket") == ticket.id
        for row in followups()
    )


def drive_session(session, coordinator, *, max_turns=None, report_path=None):
    """Drive one durable conversation; B completion never calls its proposer."""
    allowance = max_turns if max_turns is not None else session.engine.budget.max_proposer_turns
    spent = 0
    resume_jobs = getattr(coordinator, "resume", None)
    if callable(resume_jobs):
        resume_jobs()

    def write(value):
        if report_path is not None:
            write_record(report_path, research_view(value))

    def lifecycle():
        inspect = getattr(session, "lifecycle", None)
        return inspect() if callable(inspect) else None

    def finish(reason):
        terminal = getattr(session, "finish_workflow", None)
        if callable(terminal):
            value = terminal(reason=reason)
        else:
            value = session._finish("inconclusive")
        if "new_model_calls" in value:
            value["new_model_calls"] = spent
        write(value)
        return value

    while True:
        state = lifecycle()
        if state is not None and state["action"] == "deliver":
            return finish("The submitted build chain completed.")
        if state is not None and state["action"] == "wait":
            ready = coordinator.wait(timeout=30)
            if ready:
                session._collect(ready)
            continue
        remaining = allowance - spent
        if remaining <= 0:
            return finish("The research conversation reached its decision boundary.")
        if remaining <= 2 and session.state.get("pending") is None:
            converge = getattr(session, "begin_convergence", None)
            if callable(converge):
                converge()
        before = session.state['turn']
        outcome = session.run(max_turns=remaining)
        spent += outcome.get('new_model_calls', session.state['turn'] - before)
        if "new_model_calls" in outcome:
            outcome["new_model_calls"] = spent
        write(outcome)
        if outcome['status'] != 'yielded':
            if outcome["status"] in {"budget_limit", "provider_refused", "provider_blocked"}:
                return outcome  # A scheduling/provider stop is resumable, not a research verdict.
            if outcome["status"] == "context_limit":
                return finish("This conversation reached its context capacity; continue research in the next session.")
            return outcome
        while True:
            ready = coordinator.wait(timeout=0)
            if ready:
                session._collect(ready)
            state = lifecycle()
            if state is None:
                actionable = [ticket for ticket in ready
                              if _requires_proposer(ticket, coordinator.store)]
                if actionable:
                    break
                active = any(t.status in {'queued', 'running'}
                             for t in coordinator.store.tickets())
                if active:
                    ready = coordinator.wait(timeout=30)
                    if ready:
                        session._collect(ready)
                    continue
                return outcome
            if state["action"] == "deliver":
                return finish("The submitted build chain completed.")
            if state["action"] == "wake_proposer":
                if allowance - spent <= 0:
                    return finish("The research conversation reached its decision boundary.")
                break
            if state["action"] == "research":
                if allowance - spent <= 0:
                    return finish("The research conversation reached its decision boundary.")
                break
            ready = coordinator.wait(timeout=30)
            if ready:
                session._collect(ready)


def run_campaign(args, first_session, first_coordinator, *, assemble_session=None):
    """Consume B decisions in Python and start distinct, version-bound sessions."""
    from copy import copy
    factory = assemble_session or assemble
    evolution = first_session.evolution
    plan = evolution.campaign(first_session.session_key, rounds=getattr(args, 'rounds', None),
                              resume=args.resume, new_session=args.session is not None and not args.resume)
    session, coordinator = first_session, first_coordinator
    outcome = None
    for key in plan['sessions']:
        frozen = evolution.context(key)
        if frozen.get('result') is not None:
            saved = evolution.store.load_checkpoint('portfolio-session/' + key, default={})
            outcome = saved.get('workflow_result') or {'status': frozen['result']['status'], 'session_key': key}
            if outcome.get('status') == 'unresolved':
                break
            continue
        if session.session_key != key:
            coordinator.shutdown(wait=False)
            next_args = copy(args)
            next_args.session = key
            next_args.resume = evolution.store.load_checkpoint('portfolio-session/' + key, default=None) is not None
            next_args._planned_session_key = key
            session, coordinator = factory(next_args)
        try:
            coordinator.recover_running()
            outcome = drive_session(session, coordinator, max_turns=args.turns,
                                    report_path=args.output / 'session-result.json')
        finally:
            coordinator.shutdown(wait=True)
        # Draining preserves jobs even when the proposer explicitly closed.
        # It must precede selection, so late B results cannot be lost.
        if (session.state.get('workflow_result') is not None or session.state.get('closed')
                or outcome.get('status') in {'success', 'mixed', 'rejected', 'inconclusive', 'failed', 'unresolved'}):
            outcome = session.reconcile_delivery(reason='Research execution completed.')
        selection = evolution.finish(key, outcome, owned_ticket_ids=session.state.get('owned_ticket_ids', ()))
        if selection is not None:
            outcome = {**outcome, 'selected_artifact_id': selection['selected_artifact_id']}
        write_record(args.output / 'session-result.json', research_view(outcome))
        write_record(args.output / 'board.json', research_view(evolution.store.board()))
        write_record(args.output / 'accepted-artifacts.json', [
            json.loads(canonical_json(item)) for item in evolution.store.accepted_artifacts()])
        if selection is None or outcome.get('status') == 'unresolved':
            break
    coordinator.shutdown(wait=False)
    return {**(outcome or {'status': 'inconclusive'}), 'research_sessions': evolution.results()}


def write_policy_reports(session, output):
    policy = getattr(session, 'research_policy', None)
    if policy is None:
        return
    events = policy.store.policy_events()
    write_record(output / 'policy-state.json', {
        'method': policy.method, 'contract': policy.identity['contract'],
        'decisions': [row for row in events if row['kind'] == 'dispatch'],
        'feedback': [row for row in events if row['kind'] == 'feedback'],
    })
    if policy.method == 'memory':
        write_text_record(output / 'policy-memory.md', policy._memory()['content'])


def observe_proposer(args, session, coordinator):
    """Record a proposed next operation, without inventing experimental evidence."""
    class RequestedWork(Exception):
        pass
    original = session._execute_tool
    observed = []
    def execute(name, arguments, *, identity):
        observed.append({'tool': name, 'arguments': arguments, 'turn': session.state['turn']})
        write_record(args.output/'proposer-observations.json', observed)
        if name in {'experiment', 'build', 'evaluate'}:
            raise RequestedWork
        return original(name, arguments, identity=identity)
    session._execute_tool = execute
    write_record(args.output/'proposer-input.json', {'messages': session._messages(), 'tools': session.tools(),
        'model': session.engine.model, 'reasoning_effort': session.engine.reasoning_effort})
    limit = args.turns or session.engine.budget.max_proposer_turns
    try:
        while session.state['turn'] < limit:
            outcome = session.run(max_turns=limit-session.state['turn'])
            if outcome['status'] != 'yielded' or session.state['closed']:
                break
        else:
            outcome = {'status': 'turn_limit'}
    except RequestedWork:
        outcome = {'status': 'work_requested', 'decision': observed[-1],
                   'pending_tool_calls': (session.state.get('pending') or {}).get('calls', [])}
    finally:
        session._execute_tool = original
        write_record(args.output/'proposer-conversation.json', session.state['messages'])
    if coordinator.store.tickets():
        raise AssertionError('Proposer-only observation dispatched work')
    return {**outcome, 'proposer_only': True, 'worker_calls': 0, 'benchmark_executions': 0}


def main(argv=None):
    args = parser().parse_args(argv)
    if (args.proposer_only or args.experiments_only) and args.rounds not in (None, 1):
        raise ValueError('Observation modes inspect one conversation; use a new output for each independent sample')
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.env_file is not None and args.execute:
        _load_env_file(args.env_file)
    with (args.output / 'driver.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session, coordinator = assemble(args)
        try:
            if not args.execute:
                value = {'status': 'preview', 'model': session.engine.model,
                         'policy': {'method': session.research_policy.method, 'contract': session.research_policy.identity['contract']},
                         'messages': session._messages(), 'tools': session.tools(),
                         'board': research_view(coordinator.store.board())}
                write_record(args.output / 'preview.json', value)
                print(json.dumps({'status': 'preview', 'file': str(args.output / 'preview.json')}))
            else:
                if args.experiments_only:
                    from agentpilot_arena.research_observation import observe_experiments
                    value = observe_experiments(args, session, coordinator)
                else:
                    value = (observe_proposer(args, session, coordinator) if args.proposer_only
                             else run_campaign(args, session, coordinator))
                value['policy_method'] = session.research_policy.method
                write_record(args.output / 'session-result.json', research_view(value))
                print(json.dumps(research_view(value), ensure_ascii=False))
        finally:
            # Ending the conversation does not cancel an in-flight experiment.
            # The process drains its workers; results stay in the store for resume.
            coordinator.shutdown(wait=args.execute)
            if args.execute:
                write_policy_reports(session, args.output)
                write_record(args.output / 'board.json', research_view(coordinator.store.board()))
                write_record(args.output / 'accepted-artifacts.json', [
                    json.loads(canonical_json(item)) for item in coordinator.store.accepted_artifacts()
                ])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
