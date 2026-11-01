"""Strategy decisions over durable research, separate from experiment execution.

The learning unit is one dispatched build and its bound B comparison. A session
may contain many such decisions. Local experiments never supply Bernoulli reward.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import threading

from agentpilot.domain import (
    ArmSlot, ArmSpec, DirectionCandidate, DirectionMatch, DirectionMatchKind,
    DirectionSlate, EvidenceCutoff, ParentCandidate, ParentSlate, SlotKind,
    StateDescriptor, canonical_json, stable_id,
)
from agentpilot.persistence.sqlite import _dumps, _loads
from agentpilot.policy.memory.ranker import MemoryRanker
from agentpilot.policy.memory.barrier import MemoryBarrierFact
from agentpilot.policy.memory.snapshot import MemorySnapshot
from agentpilot.policy.memory.summarizer import MemorySummarizer, MemorySummaryExhausted
from agentpilot.policy.ucb.arm_allocator import ArmAllocator
from agentpilot.policy.ucb.arms import InnovationArm, ArmRegistrySnapshot
from agentpilot.policy.ucb.cooldown import ArmCooldownConfig, ArmEligibilityController
from agentpilot.policy.ucb.direction import DirectionUCB, classify_slot_adherence
from agentpilot.policy.ucb.neighborhood import NeighborhoodConfig, NeighborhoodKLUCB
from agentpilot.policy.ucb.observations import (
    ArmCreationOpportunity, BernoulliObservation, DirectionOpportunity,
    ParentOpportunity, SelectedActionRecord, UCBHistory, UCBTarget,
)
from agentpilot.policy.ucb.parent import ParentUCB
from agentpilot.search.direction_semantic_index import cosine_distance

CONTRACT = "research-policy-build-feedback/v1"
_RECORDS = {cls.__name__: cls for cls in (
    MemoryBarrierFact, ParentOpportunity, DirectionOpportunity, SelectedActionRecord,
    ArmCreationOpportunity, BernoulliObservation,
)}


def packed(value):
    if type(value).__name__ in _RECORDS and type(value) is _RECORDS[type(value).__name__]:
        fields = {name: getattr(value, name) for name in value.__dataclass_fields__}
        if isinstance(value, BernoulliObservation):
            fields["target"] = value.target.value
        return {"research_type": type(value).__name__, "payload": json.loads(_dumps(fields))}
    return json.loads(_dumps(value))


def unpacked(value):
    if isinstance(value, dict) and "research_type" in value:
        cls = _RECORDS[value["research_type"]]
        fields = _loads(json.dumps(value["payload"]))
        if cls is BernoulliObservation:
            fields["target"] = UCBTarget(fields["target"])
        return cls(**fields)
    return _loads(json.dumps(value))


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc)


class ResearchPolicy:
    """Reuse Textual ranking/summary and UCB's parent, allocation and direction kernels.

    Expensive policy calls have their own durable receipts. This lock serializes
    decisions and updates, not worker execution; the driver owns the process lock.
    """

    def __init__(self, store, initial, config, state, *, direction_factory,
                 feedback_reader, rank_completion=None, summary_completion=None,
                 encoder=None, parent_predicate=None, direction_predicate=None,
                 identity=None):
        self.store, self.initial, self.config, self.template = store, initial, config, state
        self.method = config.method.value
        self.direction_factory, self.feedback_reader = direction_factory, feedback_reader
        self.encoder = encoder
        self.lock = threading.RLock()
        self.session_key = None
        self.parent_predicate = parent_predicate
        self.direction_predicate = direction_predicate
        self.identity = {"contract": CONTRACT, "method": self.method, "settings": json.loads(canonical_json(config)),
                         "environment": packed(state.compatibility),
                         "parent_descriptor": packed(state.parent_descriptor),
                         "direction_descriptor": packed(state.direction_descriptor),
                         "parent_predicate": getattr(parent_predicate, '__module__', '') + '.' + getattr(parent_predicate, '__qualname__', ''),
                         "direction_predicate": getattr(direction_predicate, '__module__', '') + '.' + getattr(direction_predicate, '__qualname__', ''),
                         **(identity or {})}
        if self.method == "memory":
            self.ranker = MemoryRanker(rank_completion)
            self.summarizer = MemorySummarizer(summary_completion, max_chars=config.memory.max_chars)
        elif self.method == "ucb":
            if encoder is None or parent_predicate is None or direction_predicate is None:
                raise ValueError("UCB research requires its frozen semantic encoder and neighborhood predicates")
            actual, semantic = encoder.identity, config.ucb.semantic
            if (actual.encoder_id != f'{semantic.provider}:{semantic.model}'
                    or actual.encoder_identity != semantic.encoder_identity
                    or actual.dimension != semantic.dimension or actual.distance != semantic.metric):
                raise ValueError('UCB encoder differs from its configured calibration identity')
        else:
            raise ValueError("Research requires the configured UCB or Memory strategy")
        self.store.put_document("research-policy/identity", self.identity)

    def _events(self, kind):
        return self.store.policy_events(kind)

    def _cutoff(self):
        events = self.store.policy_events()
        return events[-1]['sequence'] if events else 0

    def _memory(self):
        return self.store.load_checkpoint("research-policy/memory", {"content": "", "seen": []})

    def _snapshot(self, key, content=None):
        return MemorySnapshot(self.store.namespace, key, key,
                              Path(self.store.path).parent / "policy-memory.md",
                              self._memory()["content"] if content is None else content)

    def _state(self, key, *, parent=None):
        return replace(self.template, state_id=key, run_id=self.store.namespace, batch_id=key,
                       cutoff=EvidenceCutoff(datetime.now(timezone.utc)),
                       archive_seq=self._cutoff(), budget={},
                       parent_descriptor=self._parent_descriptor(parent) if parent else self.template.parent_descriptor)

    def _parent_descriptor(self, artifact_id):
        # Never copy the original baseline's measured behavior onto an unmeasured
        # child. Exact source identity remains available until new A evidence exists.
        artifact = self.store.artifact(artifact_id)
        raw = deepcopy(json.loads(canonical_json(self.template.parent_descriptor.blocks)))
        raw.setdefault("program", {})["research_commit"] = artifact.commit_sha
        if artifact_id != self.initial:
            for name in tuple(raw):
                if "behavior" in name or "failure_regime" in name:
                    raw[name] = {"measurement": "unmeasured"}
        return StateDescriptor(raw)

    def _history(self, sequence=None):
        history = UCBHistory()
        for event in self._events("dispatch"):
            if sequence is not None and event['sequence'] > sequence:
                continue
            data = event["value"]
            for name, method in (("parent_opportunity", history.record_parent_opportunity),
                                 ("direction_opportunity", history.record_direction_opportunity),
                                 ("selected_action", history.record_selected_action),
                                 ("creation", history.record_creation_transfer)):
                if data.get(name) is not None:
                    method(unpacked(data[name]))
        for event in self._events("feedback"):
            if sequence is not None and event['sequence'] > sequence:
                continue
            for row in event["value"].get("observations", []):
                history.record_observation(unpacked(row))
        return history

    def _estimators(self, sequence=None):
        raw = self.config.ucb.neighborhood
        config = NeighborhoodConfig(raw.fallback_levels, raw.min_observations,
                                    raw.exploration_c, raw.solver_tolerance,
                                    raw.solver_max_iterations, raw.tie_tolerance)
        history = self._history(sequence)
        return (NeighborhoodKLUCB(history, self.parent_predicate, config),
                NeighborhoodKLUCB(history, self.direction_predicate, config))

    def _cooldowns(self, state, sequence):
        raw = self.config.ucb.cooldown
        controller = ArmEligibilityController(ArmCooldownConfig(
            raw.max_slot_generation_failures, raw.slot_generation_cooldown_opportunities,
            raw.max_retryable_skips, raw.skip_cooldown_opportunities))
        dispatches = {e['value']['ticket_id']: e['value'] for e in self._events('dispatch') if e['sequence'] <= sequence}
        registry = {}
        for ticket_id, decision in dispatches.items():
            row = self.store.load_document('research-policy/candidate/' + ticket_id)
            registry.setdefault(decision['arm_id'], InnovationArm(decision['arm_id'], state.compatibility,
                ArmSpec(row['instruction'][:2400].strip()), unpacked(row['direction']), ticket_id, timestamp(row['created_at'])))
        for event in self._events('feedback'):
            if event['sequence'] > sequence or event['value']['ticket_id'] not in dispatches:
                continue
            row = event['value']
            decision = dispatches[row['ticket_id']]
            controller.record_round(decision_round_id=decision['decision_key'], compatibility=state.compatibility,
                generation_succeeded={}, selected_arm_id=decision['arm_id'], retryable_skip=row['reward'] is None)
        snapshot = ArmRegistrySnapshot(state.compatibility, state.cutoff, tuple(registry.values()))
        return controller.report(snapshot, base_arm_ids=tuple(registry))

    def _rank(self, key, stage, ids, candidates, *, memory=None, evidence=None):
        saved = self.store.load_document('research-policy/rank-input/' + key)
        if saved is None:
            saved = {'stage': stage, 'ids': list(ids), 'candidates': candidates,
                     'memory': self._memory()['content'] if memory is None else memory,
                     'evidence': {**(evidence or {}), 'local_experiments': self._local_experiments()}}
            self.store.put_document('research-policy/rank-input/' + key, saved)
        stage, ids, candidates, memory, evidence = (saved[name] for name in ('stage', 'ids', 'candidates', 'memory', 'evidence'))
        if len(ids) > 64:
            winners = []
            for offset in range(0, len(ids), 64):
                part = [{**row, "index": i} for i, row in enumerate(candidates[offset:offset + 64])]
                result = self._rank(key + '/' + str(offset), stage, ids[offset:offset + 64], part,
                                    memory=memory, evidence=evidence)
                winners.append(result[0])
            rows = [{**candidates[ids.index(identity)], "index": i} for i, identity in enumerate(winners)]
            winner = self._rank(key + '/final', stage, winners, rows, memory=memory, evidence=evidence)[0]
            return (winner, *(identity for identity in ids if identity != winner))
        candidates = [{name: self._clip(value) if isinstance(value, str) else value
                       for name, value in row.items()} for row in candidates]
        return self.ranker.rank_research(
            snapshot=self._snapshot(key, memory), slate_id=key, stage=stage,
            candidate_ids=tuple(ids), candidates=candidates,
            evidence=evidence,
            objective=f'{self.template.compatibility.metric_direction} the configured primary metric',
        )

    def begin(self, session_key, *, persist=True):
        with self.lock:
            key = "research-policy/session/" + session_key
            saved = self.store.load_document(key)
            if saved is not None:
                self.session_key = session_key
                return deepcopy(saved)
            if persist:
                self.sync()
            eligible = {e['value']['public']['candidate'] for e in self._events('feedback')
                        if e['value']['reward'] is not None and e['value']['public']['admission'] == 'admitted'}
            parents = [self.store.artifact(self.initial), *(p for p in self.store.accepted_artifacts() if p.id in eligible)]
            by_commit = {}
            for parent in parents:
                by_commit.setdefault(parent.commit_sha, parent)
            parents = list(by_commit.values())
            ids = [p.id for p in parents]
            state = self._state(session_key)
            slate = ParentSlate(session_key, session_key, state.state_id, tuple(
                ParentCandidate(p.id, p.commit_sha, self._parent_descriptor(p.id)) for p in parents))
            memory = self._memory()["content"]
            if not persist:
                selected, trace = ids[0], {"selection": "preview; no strategy calls dispatched"}
            elif self.method == "ucb":
                parent, _ = self._estimators()
                choice = ParentUCB(parent).select(slate, state, seed=self._seed(session_key))
                selected, trace = choice.parent_id, packed(choice.trace)
            else:
                ranking = self._rank(session_key, "parent", ids, [
                    {"index": i, "artifact": p.id, "source": p.commit_sha, "change": p.metadata.get("instruction", "Initial program")}
                    for i, p in enumerate(parents)], memory=memory,
                    evidence={"comparisons": self._public_feedback()})
                selected, trace = ranking[0], {"ranking": list(ranking)}
            self.session_key = session_key
            directions = self._research_directions(selected) if persist else []
            result = {"method": self.method, "base_artifact_id": selected, "memory": memory,
                      "directions": directions, "parent_selection": trace,
                      "feedback_cutoff": self._cutoff()}
            if persist:
                self.store.put_document(key, result)
                self.session_key = session_key
            return deepcopy(result)

    def _seed(self, key):
        base = self.config.ucb.base_seed if self.method == "ucb" else 0
        return int(stable_id("research-policy-seed", base, key).split(":")[-1][-12:], 16)

    def _public_feedback(self):
        rows = []
        for event in self._events("feedback")[-16:]:
            source = event['value']['public']
            rows.append({**source, 'instruction': self._clip(source['instruction']),
                         'comparison': {k: v for k, v in (source.get('comparison') or {}).items()
                                        if k != 'challenger_absolute_value'},
                         'changes': source['changes'][:8], 'omitted_changes': max(0, len(source['changes']) - 8)})
        return rows

    @staticmethod
    def _clip(text, size=512):
        text = str(text)
        encoded = text.encode('utf-8')
        return text if len(encoded) <= size else encoded[:size].decode('utf-8', 'ignore') + '…'

    def _local_experiments(self):
        judgments = {line['track']: line.get('latest_judgment') for line in self.store.research_lines()}
        return [{'ticket': t.id, 'question': self._clip(t.arguments.get('instruction', '')),
                 'status': t.status, 'interpretation': 'local evidence, not B effectiveness',
                 'proposer_judgment': self._clip((judgments.get(t.track_id) or {}).get('text', '')),
                 'judgment_source_commit': (judgments.get(t.track_id) or {}).get('source_commit'),
                 'summary': self._clip((t.result or {}).get('summary', ''))}
                for t in self.store.tickets() if t.kind == 'experiment' and t.status in {'succeeded', 'failed'}][-8:]

    def _research_directions(self, parent):
        registrations = [event["value"] for event in self._events("candidate")]
        active = {track.id for track in self.store.tracks() if track.state == "active"}
        examples = {}
        for row in registrations:
            if row["track_id"] in active:
                examples[row.get("arm_id") or row["track_id"]] = row
        if not examples:
            return []
        if self.method == "ucb":
            _, estimator = self._estimators()
            state = self._state("research-context/" + str(self.session_key), parent=parent)
            scored = [(estimator.estimate(target=UCBTarget.DIRECTION, compatibility=state.compatibility,
                        descriptor=state.direction_descriptor, cutoff=state.cutoff, arm_id=arm).index, arm)
                      for arm in examples]
            order = [arm for _, arm in sorted(scored, reverse=True)]
            limit = self.config.ucb.direction_slate_size
        else:
            order, limit = list(reversed(examples)), self.config.memory.direction_slate_size
        return [{"track": examples[arm]["track_id"], "instruction": self._clip(examples[arm]["instruction"], 1024),
                 "open": {"tool": "read_experiment", "arguments": {"ticket": examples[arm]["ticket_id"]}}}
                for arm in order[:limit]]

    def register(self, ticket):
        if ticket.kind != "build":
            return
        with self.lock:
            key = "research-policy/candidate/" + ticket.id
            saved = self.store.load_document(key)
            if saved is not None:
                self.store.policy_event('candidate/' + ticket.id, 'candidate', saved)
                return
            if self.session_key is None:
                raise ValueError("A build needs its frozen research strategy context")
            context = self.store.load_document("research-policy/session/" + self.session_key)
            direction = self.direction_factory(ticket)
            row = {"ticket_id": ticket.id, "session": self.session_key, "track_id": ticket.track_id,
                   "created_at": ticket.created_at,
                   "source_commit": ticket.arguments['base']['commit_sha'],
                   "parent_id": context["base_artifact_id"], "instruction": ticket.arguments["instruction"],
                   "direction": packed(direction), "arm_id": None, "embedding": None}
            if self.method == "ucb":
                # An arm description is a hypothesis, not a certified cause.
                base_id = ticket.arguments['base'].get('artifact_id', ticket.arguments['base'].get('id'))
                ancestry, visited = [], set()
                while base_id and base_id != row['parent_id']:
                    if base_id in visited:
                        raise ValueError('Candidate source ancestry contains a cycle')
                    visited.add(base_id)
                    base = self.store.artifact(base_id)
                    ancestry.append(base.metadata.get('instruction') or ('Source branch ' + base.commit_sha))
                    base_id = base.base_id
                hypothesis = '\n'.join([*reversed(ancestry), row['instruction']])
                row['arm_hypothesis'] = hypothesis
                row['semantic_truncated'] = len(hypothesis) > 2400
                encoded = self.encoder.encode_arm(ArmSpec(failure_mechanism=hypothesis[:2400].strip()))
                row["embedding"] = list(encoded.embedding)
                rows = self._events("candidate")
                distances = sorted((cosine_distance(row["embedding"], e["value"]["embedding"]),
                                    e["value"]["arm_id"]) for e in rows if not e['value'].get('semantic_truncated'))
                semantic = self.config.ucb.semantic
                near = [x for x in distances if x[0] < semantic.tau_arm - semantic.threshold_boundary_margin] if not row['semantic_truncated'] else []
                ties = {arm for distance, arm in near if distance - near[0][0] <= semantic.nearest_arm_tie_tolerance} if near else set()
                row["arm_id"] = next(iter(ties)) if len(ties) == 1 else stable_id("research-arm", hypothesis)
                row['arm_match'] = 'existing' if len(ties) == 1 else 'new' if not ties else 'ambiguous; kept separate'
                row['arm_distance'] = near[0][0] if near else None
                realization = self.encoder.encode_realization(direction)
                row['realization_embedding'] = list(realization.embedding)
                repeats = [(cosine_distance(realization.embedding, e['value']['realization_embedding']), e['value']['ticket_id'])
                           for e in rows if e['value'].get('realization_embedding')
                           and e['value'].get('source_commit') == row['source_commit']]
                repeats = sorted(item for item in repeats if item[0] < semantic.tau_repeat - semantic.threshold_boundary_margin)
                row['semantic_repeat_of'] = repeats[0][1] if repeats else None
            self.store.put_document(key, row)
            self.store.policy_event("candidate/" + ticket.id, "candidate", row)

    def choose(self, tickets):
        """Choose one available worker investment; other candidates stay queued."""
        with self.lock:
            self.sync()
            tickets = sorted(tickets, key=lambda t: (t.created_at, t.id))
            pending = self.store.load_checkpoint('research-policy/pending-dispatch')
            if pending and any(e['value'].get('decision_key') == pending['key'] for e in self._events('dispatch')):
                self.store.checkpoint('research-policy/pending-dispatch', None)
                pending = None
            for ticket in tickets:
                existing = next((e["value"] for e in self._events("dispatch") if e["value"]["ticket_id"] == ticket.id), None)
                if existing is not None:
                    return ticket.id
            if pending:
                ids = {t.id for t in tickets}
                if not set(pending['tickets']) <= ids:
                    raise ValueError('Pending policy decision must be reconciled with its original queued candidates')
                tickets = [self.store.get_ticket(identity) for identity in pending['tickets']]
            rows = [self.store.load_document("research-policy/candidate/" + t.id) for t in tickets]
            if any(row is None for row in rows):
                raise ValueError("Unregistered build cannot bypass the research strategy")
            key = pending['key'] if pending else stable_id("research-dispatch", self.store.namespace, self._cutoff(), [t.id for t in tickets])
            if pending is None:
                pending = {'key': key, 'tickets': [t.id for t in tickets],
                           'state': packed(self._state(key)), 'cutoff': self._cutoff()}
                self.store.checkpoint('research-policy/pending-dispatch', pending)
            state = unpacked(pending['state'])
            ids = tuple(t.id for t in tickets)
            record = {"ticket_id": None, "decision_key": key, "eligible_tickets": list(ids), "cutoff": pending['cutoff'],
                      "parent_opportunity": None, "direction_opportunity": None,
                      "selected_action": None, "creation": None}
            if self.method == "memory":
                ranking = self._rank(key, "direction", ids, [
                    {"index": i, "ticket": row['ticket_id'], "instruction": row["instruction"], "parent": row["parent_id"]}
                    for i, row in enumerate(rows)], evidence={"comparisons": self._public_feedback()})
                chosen = ranking[0]
                record["ranking"] = list(ranking)
            else:
                parent_estimator, estimator = self._estimators(pending['cutoff'])
                parents = sorted({row["parent_id"] for row in rows})
                slate = ParentSlate(key, key, key, tuple(ParentCandidate(p, self.store.artifact(p).commit_sha,
                                                self._parent_descriptor(p)) for p in parents))
                parent = ParentUCB(parent_estimator).select(slate, state, seed=self._seed(key))
                rows = [row for row in rows if row["parent_id"] == parent.parent_id]
                dispatches = [e for e in self._events('dispatch') if e['sequence'] <= pending['cutoff']]
                dispatched = {e["value"]["arm_id"] for e in dispatches}
                cooldown = self._cooldowns(state, pending['cutoff'])
                preferred = [row for row in rows if row['arm_id'] not in dispatched or row['arm_id'] in cooldown.eligible_arm_ids]
                record['cooldown'] = json.loads(canonical_json(cooldown))
                record['cooldown_fallback'] = not bool(preferred)
                # Cooldown deprioritizes a line but does not deadlock an otherwise
                # empty asynchronous queue or discard a requested experiment.
                rows = preferred or rows
                distinct = [row for row in rows if not row.get('semantic_repeat_of')]
                record['semantic_repeat_fallback'] = not bool(distinct)
                rows = distinct or rows
                existing = tuple(sorted({row["arm_id"] for row in rows if row["arm_id"] in dispatched}))
                new = any(row["arm_id"] not in dispatched for row in rows)
                capacity = self.config.ucb.direction_slate_size
                allocation = ArmAllocator(estimator).allocate(state=state, parent_id=parent.parent_id,
                    decision_round_id=key, eligible_arm_ids=existing, cold_start=not existing,
                    generation_capacity=capacity, discover_quota=self.config.ucb.discover_quota, seed=self._seed(key))
                allowed = {slot.source_arm_id for slot in allocation.slots if slot.kind is SlotKind.EXISTING}
                eligible = [row for row in rows if row["arm_id"] in allowed or row["arm_id"] not in dispatched]
                # No queued discovery candidate means existing work remains useful.
                eligible = eligible or rows
                # Retain a discovery investment in every configured window when
                # a new candidate is actually ready; do not manufacture a proposal.
                position = len(dispatches) % capacity
                if new and position < self.config.ucb.discover_quota:
                    eligible = [row for row in eligible if row['arm_id'] not in dispatched]
                candidates = []
                for i, row in enumerate(eligible):
                    old = row["arm_id"] in dispatched
                    slot = ArmSlot(stable_id("research-slot", key, row["ticket_id"]),
                                   SlotKind.EXISTING if old else SlotKind.DISCOVER, i,
                                   row["arm_id"] if old else None)
                    match = DirectionMatch(DirectionMatchKind.EXISTING if old else DirectionMatchKind.NEW,
                                           distance=None, arm_id=row["arm_id"])
                    candidates.append(DirectionCandidate(row["ticket_id"], slot, unpacked(row["direction"]),
                                                         match, classify_slot_adherence(slot, match)))
                direction_slate = DirectionSlate(key, key, key, parent.parent_id, tuple(candidates))
                selection = DirectionUCB(estimator).select(direction_slate, state, seed=self._seed(key))
                chosen = selection.candidate_id
                selected = next(c for c in candidates if c.candidate_id == chosen)
                arm = selected.match.arm_id
                now = state.cutoff.available_at
                record.update({"arm_id": arm, "parent_id": parent.parent_id,
                    "slot_kind": selected.source_slot.kind.value, "match_kind": selected.match.kind.value,
                    "parent_opportunity": packed(ParentUCB.opportunity(parent, slate, state, available_at=now)),
                    "direction_opportunity": packed(DirectionOpportunity(stable_id("direction-opportunity", key), key,
                        existing, new, state.compatibility, state.direction_descriptor, now)),
                    "selected_action": packed(SelectedActionRecord(stable_id("selected-action", chosen), key, chosen, now)),
                    "creation": packed(ArmCreationOpportunity(stable_id("creation", chosen), key, chosen, arm,
                        state.compatibility, state.direction_descriptor, now)) if selected.source_slot.kind is SlotKind.DISCOVER else None,
                    "allocation": packed(allocation.trace), "direction_selection": packed(selection.trace)})
            record["ticket_id"] = chosen
            self.store.policy_event("dispatch/" + chosen, "dispatch", record)
            self.store.checkpoint('research-policy/pending-dispatch', None)
            return chosen

    def reuse(self, ticket, *, recovery=False):
        """Reuse only byte-identical frozen build inputs, never semantic guesses."""
        if ticket.kind != 'build':
            return None
        key = 'research-policy/reuse/' + ticket.id
        frozen = self.store.load_document(key)
        if frozen is not None:
            return deepcopy(frozen['result'])
        if recovery:
            return None
        result = None
        for previous in self.store.tickets():
            if (previous.id != ticket.id and previous.kind == 'build' and previous.status == 'succeeded'
                    and previous.artifact_id and previous.arguments == ticket.arguments):
                artifact = self.store.artifact(previous.artifact_id)
                result = {'cost_usd': 0., 'summary': 'Identical frozen implementation reused; its new B comparison still runs.',
                        'artifact': {'commit_sha': artifact.commit_sha, 'base_id': artifact.base_id,
                                     'config': artifact.config, 'metadata': {**artifact.metadata,
                                         'reused_from': previous.artifact_id, 'build_ticket': ticket.id}}}
                break
        self.store.put_document(key, {'result': result})
        return result

    def sync(self):
        with self.lock:
            seen = {e["value"]["ticket_id"] for e in self._events("feedback")}
            plans = {r['build_ticket']: r for r in self.store.followups() if r.get('kind') == 'admit'}
            followups = {identity: row.get('ticket') for identity, row in plans.items()}
            for event in self._events("dispatch"):
                decision = event["value"]
                identity = decision["ticket_id"]
                if identity in seen:
                    continue
                build = self.store.get_ticket(identity)
                admit = self.store.get_ticket(followups[identity]) if followups.get(identity) else None
                if build.status in {"queued", "running", "accounting_unknown"} or (
                    admit is not None and admit.status in {"queued", "running", "accounting_unknown"}):
                    continue
                if build.status == "succeeded" and admit is None and plans.get(identity, {}).get('status') != 'skipped':
                    continue
                candidate = self.store.load_document("research-policy/candidate/" + identity)
                feedback = self.feedback_reader(build, admit, candidate)
                observations = []
                if self.method == "ucb" and feedback["reward"] is not None:
                    opportunity = unpacked(decision["parent_opportunity"])
                    direction = unpacked(decision["direction_opportunity"])
                    targets = [UCBTarget.PARENT, UCBTarget.DIRECTION]
                    if decision["slot_kind"] == "discover":
                        targets.append(UCBTarget.DISCOVER)
                    for target in targets:
                        observations.append(packed(BernoulliObservation(
                            stable_id("research-observation", identity, target.value), identity,
                            opportunity.decision_round_id, target, feedback["reward"], candidate["parent_id"],
                            None if target is UCBTarget.PARENT else decision["arm_id"],
                            SlotKind(decision["slot_kind"]), DirectionMatchKind(decision["match_kind"]),
                            opportunity.compatibility, opportunity.selected_parent_descriptor if target is UCBTarget.PARENT else direction.descriptor,
                            timestamp(feedback["completed_at"]))))
                self.store.policy_event("feedback/" + identity, "feedback", {
                    "ticket_id": identity, **feedback, "observations": observations})
            if self.method == "memory":
                while True:
                    current = self._memory()
                    pending = [e for e in self._events("feedback") if e["event_id"] not in current["seen"]]
                    plan = self.store.load_checkpoint('research-policy/pending-memory')
                    if plan and set(plan['events']) <= set(current['seen']):
                        self.store.checkpoint('research-policy/pending-memory', None)
                        plan = None
                    if not pending and plan is None:
                        break
                    if plan is None:
                        batch = pending[:8]
                        key = stable_id("research-memory-update", current, [e["event_id"] for e in batch])
                        plan = {'key': key, 'events': [e['event_id'] for e in batch], 'memory': current}
                        self.store.checkpoint('research-policy/pending-memory', plan)
                    else:
                        by_id = {e['event_id']: e for e in self._events('feedback')}
                        batch = [by_id[identity] for identity in plan['events']]
                    key, current = plan['key'], plan['memory']
                    pending_ids = [e['event_id'] for e in pending]
                    failure = self.store.load_document('research-policy/summary-failure/' + key)
                    if failure is not None:
                        if failure['pending_events'] == pending_ids:
                            break  # No busy retries of exhausted durable model receipts.
                        key = stable_id('research-memory-retry', key, pending_ids)
                        plan = {**plan, 'key': key}
                        self.store.checkpoint('research-policy/pending-memory', plan)
                    facts = [unpacked(e["value"]["memory_fact"]) for e in batch if e["value"].get("memory_fact")]
                    try:
                        content = self.summarizer.summarize(self._snapshot(key, current["content"]), facts,
                            completed_cutoff=EvidenceCutoff(datetime.now(timezone.utc)), update_id=key) if facts else current["content"]
                    except MemorySummaryExhausted as error:
                        self.store.put_document('research-policy/summary-failure/' + key, {
                            'error': str(error), 'pending_events': pending_ids, 'events': plan['events'],
                            'recorded_at': datetime.now(timezone.utc).isoformat(),
                        })
                        # Keep the last validated memory and all unsummarized facts.
                        # New feedback permits another bounded attempt; research continues.
                        break
                    current = {"content": content, "seen": current["seen"] + [e["event_id"] for e in batch]}
                    self.store.checkpoint("research-policy/memory", current)
                    self.store.checkpoint('research-policy/pending-memory', None)

    def context_text(self, context):
        return {"strategy": self.method, "lessons": context["memory"], "directions_to_consider": context["directions"],
                "guidance": "Past findings are provisional. You may pursue another design or a new direction; queued candidates are retained."}
