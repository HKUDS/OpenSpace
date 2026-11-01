"""Measure and validate a reusable semantic calibration for research directions."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import fcntl
import hashlib
import json
import os

from agentpilot.app.cli import _load_env_file
from agentpilot.app.semantic_encoder import ProviderDirectionEmbeddingBackend
from agentpilot.domain import canonical_json, stable_id
from agentpilot.persistence import SQLiteUnitOfWorkFactory
from agentpilot.providers.adapters import OpenRouterEmbeddingAdapter
from agentpilot.providers.embedding_accounting import EmbeddingAccountingReconciler, OpenRouterEmbeddingAnalyticsLookup
from agentpilot.research import ResearchStore
from agentpilot.search.semantic_calibration import (
    derive_semantic_thresholds, evaluate_semantic_calibration_bundle,
    parse_semantic_calibration_bundle, parse_semantic_calibration_manifest,
)
from agentpilot.search.semantic_research_calibration import PROTOCOL
from agentpilot_arena.runtime.research_policy import MeteredPolicyEmbedding
from agentpilot_arena.runtime.research_worker import write_record

MODEL = 'qwen/qwen3-embedding-8b'
ENCODER = 'openrouter-qwen3-embedding-8b-float4096-research-v1'
DATA = Path(__file__).parent / 'data' / 'research_semantic_cases.json'
BUNDLE = 'research-semantic-dual-view-v1'


def examples(dataset, split):
    rows = []
    labels = {'paraphrase': 'repeat_paraphrase', 'novel': 'same_arm_novel_realization', 'distinct': 'distinct_mechanism'}
    for group in dataset['groups']:
        if group['split'] != split:
            continue
        for view in ('arm', 'realization'):
            def endpoint(name):
                payload = {'failure_mechanism': group[name]} if view == 'arm' else {
                    'implementation_direction': group[name], 'resolved_scope': dataset['scope'], 'new_paths': []}
                return {'item_id': f'{group["id"]}/{view}/{name}', 'canonical_input': canonical_json(payload)}
            for name, label in labels.items():
                rows.append({'case_id': f'{group["id"]}/{view}/{name}', 'group_id': group['id'],
                             'label': label, 'annotation': group['annotation'],
                             'anchor': endpoint('anchor'), 'candidate': endpoint(name)})
    return rows


def manifest(dataset, split, vectors, thresholds, *, measured=True):
    cases = examples(dataset, split)
    for case in cases:
        for name in ('anchor', 'candidate'):
            case[name]['embedding'] = vectors[case[name]['canonical_input']]
    return {'manifest_id': BUNDLE + '/' + split, 'split': split,
        'provenance': 'target_model_labeled' if measured else 'unit_test',
        'embedding_identity': {'encoder_id': 'openrouter:' + MODEL, 'encoder_identity': ENCODER,
                               'dimension': 4096, 'distance': 'cosine'},
        'thresholds': thresholds, 'requirements': {'minimum_cases_per_label': dataset['minimum_cases_per_label']},
        'annotators': dataset['annotators'], 'minimum_annotators': dataset['minimum_annotators'],
        'adjudication_policy': dataset['annotation_source'],
        'embedding_source': ('Actual OpenRouter embedding receipts; raw requests, responses and settled costs retained with the calibration run.'
                             if measured else 'Injected test encoder; no measured provider provenance.'),
        'threshold_selection_protocol': PROTOCOL,
        'selection_fixture_id': BUNDLE + '/calibration' if split == 'acceptance' else None,
        'cases': cases}


def run(output, *, env_file, execute=False, budget=.1, dataset_path=DATA, phase='all', embedding=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    dataset_bytes = Path(dataset_path).read_bytes()
    dataset = json.loads(dataset_bytes)
    store = ResearchStore(output / 'accounting.sqlite3', budget_usd=budget)
    measured = embedding is None
    plan = {'dataset_sha256': hashlib.sha256(dataset_bytes).hexdigest(), 'model': MODEL,
            'encoder_identity': ENCODER, 'batch_size': 16, 'budget_usd': budget,
            'selection_protocol': PROTOCOL, 'measured_provider': measured}
    store.put_document('calibration-plan/' + PROTOCOL, plan)
    store.put_document('labeled-cases', dataset)
    write_record(output / PROTOCOL / 'plan.json', plan)
    if not execute:
        return {'status': 'prepared', 'selection_pairs': len(examples(dataset, 'calibration')),
                'acceptance_pairs': len(examples(dataset, 'acceptance')), 'paid_calls': 0}
    with (output / 'driver.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if embedding is None:
            _load_env_file(Path(env_file))
            key, management = os.environ['OPENROUTER_API_KEY'], os.environ['OPENROUTER_MANAGEMENT_KEY']
            def create(**payload):
                import httpx
                identity = stable_id('calibration-request', payload)
                folder = output / 'requests' / identity
                write_record(folder / 'request.json', payload)
                response_path = folder / 'response.json'
                if response_path.exists():
                    return json.loads(response_path.read_text())
                response = httpx.post('https://openrouter.ai/api/v1/embeddings',
                    json=payload, headers={'Authorization': 'Bearer ' + key}, timeout=120)
                write_record(folder / 'http.json', {'status': response.status_code})
                response.raise_for_status()
                body = response.json()
                write_record(response_path, body)
                return body
            client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
            inner = ProviderDirectionEmbeddingBackend(provider=OpenRouterEmbeddingAdapter(client), model=MODEL,
                encoder_identity=ENCODER, dimension=4096,
                unit_of_work=SQLiteUnitOfWorkFactory(output / 'provider.sqlite3'),
                run_id=store.namespace, batch_id='calibration',
                accounting_reconciler=EmbeddingAccountingReconciler(OpenRouterEmbeddingAnalyticsLookup(management_key=management, model=MODEL)))
            embedding = MeteredPolicyEmbedding(store, inner, .01)
        def encode(split):
            rows = examples(dataset, split)
            inputs = list(dict.fromkeys(endpoint['canonical_input'] for row in rows for endpoint in (row['anchor'], row['candidate'])))
            result = {}
            for start in range(0, len(inputs), 16):
                batch = inputs[start:start + 16]
                key = stable_id('calibration-vectors' if measured else 'test-calibration-vectors', batch)
                cached = store.load_document(key)
                if cached is None:
                    cached = list(map(list, embedding.embed(batch)))
                    store.put_document(key, cached)
                result.update(zip(batch, cached, strict=True))
            return result
        thresholds = {'threshold_id': BUNDLE, 'tau_repeat': .08, 'tau_arm': .22,
                      'threshold_boundary_margin': dataset['boundary_margin'], 'nearest_arm_tie_tolerance': 1e-12}
        selected_vectors = encode('calibration')
        selected = manifest(dataset, 'calibration', selected_vectors, thresholds, measured=measured)
        derived = derive_semantic_thresholds(parse_semantic_calibration_manifest(selected))
        write_record(output / PROTOCOL / 'selection-derivation.json', asdict(derived))
        failures = [r for r in derived.reasons if r != 'configured_taus_differ_from_selection_derivation']
        if failures or derived.derived_tau_repeat is None or derived.derived_tau_arm is None:
            return {'status': 'selection_failed', 'reasons': list(derived.reasons), 'accepted': False,
                    'cost_usd': store.board()['budget']['spent_usd']}
        thresholds = {**thresholds, 'tau_repeat': derived.derived_tau_repeat, 'tau_arm': derived.derived_tau_arm}
        store.put_document('selected-thresholds/' + PROTOCOL, thresholds)
        write_record(output / 'thresholds.json', thresholds)
        selected['thresholds'] = thresholds
        write_record(output / 'selection.json', selected)
        if phase == 'selection':
            return {'status': 'selection_complete', 'thresholds': thresholds, 'accepted': False,
                    'cost_usd': store.board()['budget']['spent_usd']}
        accepted = manifest(dataset, 'acceptance', encode('acceptance'), thresholds, measured=measured)
        bundle = {'bundle_id': BUNDLE, 'selection': selected, 'acceptance': accepted}
        report = evaluate_semantic_calibration_bundle(parse_semantic_calibration_bundle(bundle))
        write_record(output / 'report.json', asdict(report))
        write_record(output / 'measured.bundle.json', bundle)
        if report.formal_accepted:
            write_record(output / 'accepted.bundle.json', bundle)
        result = {'status': 'accepted' if report.formal_accepted else 'validation_failed',
                  'accepted': report.formal_accepted, 'reasons': list(report.reasons),
                  'cost_usd': store.board()['budget']['spent_usd'],
                  'bundle': str(output / 'accepted.bundle.json') if report.formal_accepted else None}
        write_record(output / 'result.json', result)
        return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--env-file', type=Path, default=Path('.env'))
    p.add_argument('--dataset', type=Path, default=DATA)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--phase', choices=('selection','all'), default='all')
    args = p.parse_args(argv)
    result = run(args.output, env_file=args.env_file, execute=args.execute, dataset_path=args.dataset, phase=args.phase)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in {'prepared','selection_complete','accepted'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
