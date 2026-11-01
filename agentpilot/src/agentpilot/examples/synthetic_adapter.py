"""Deterministic synthetic adapter for examples and interface exploration.

A public example should run without private benchmark data, external worker
software, or paid providers. This adapter keeps the production assembly, batch
loop, and persistence path untouched and replaces the three external seams --
completion, embedding, and action runner -- with deterministic stand-ins.

Directions are generated in 32 mechanism families. A family maps to one
basis vector of a 65-dimensional embedding space and each realization inside a
family rotates towards the paired basis vector, so family members land inside
``tau_arm`` while different families stay orthogonal.  That is what makes the
run produce several innovation arms, repeats and discoveries instead of one
degenerate arm.

Outcomes rotate through every fate the effectiveness rule can assign, including
the three skip paths, so admission, lineage and policy views all receive
non-empty data.
"""

from __future__ import annotations

import ast
import json
import math
import random
import subprocess
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agentpilot.app import ExecutionResult
from agentpilot.app.assembly import (
    ActionRunnerBinding,
    AssemblyDependencies,
    BatchAssemblyContext,
    Environment,
    NamedApplicabilityBuilder,
    NamedNearStatePredicate,
    ProviderRegistry,
    assert_action_execution_fence_live,
)
from agentpilot.app.search_read_model import (
    CanonicalSearchReadModelWriter,
    DurableSearchInputSource,
)
from agentpilot.domain import (
    ActionExecutionAttestation,
    ActionExecutionMode,
    ChildProgramReadout,
    CompatibilityKey,
    EvaluationValidity,
    EvidenceTier,
    ImplementationAdherence,
    OutcomeAttribution,
    PairedOutcome,
    ParentCandidate,
    ParentRepositoryMap,
    PolicyAction,
    RepositoryFileMap,
    RepositorySymbol,
    SearchState,
    StateDescriptor,
    WorkerExecutionStatus,
    WorkerResult,
    canonical_json,
    stable_id,
    utc_now,
)
from agentpilot.evaluation import (
    UNIFORM_ESTIMATOR_CONTRACT,
    EffectivenessRule,
    EvaluationCell,
    FrozenActionEvaluationPlan,
    FrozenEvaluationBatch,
    FrozenInformationPriorSnapshot,
    FrozenPairedPlan,
)
from agentpilot.providers import (
    AccountingStatus,
    EmbeddingResult,
    ProviderReceipt,
    ProviderUsage,
)
from agentpilot.search import DirectionApplicability, parse_semantic_calibration_bundle
from agentpilot.domain.trace_evidence import (
    ReadResult,
    TraceArtifactRef,
    TraceCaptureCoordinates,
    TraceCaptureRecord,
    TraceCaptureResult,
    TraceCaptureStatus,
    TraceCaseIndex,
    TraceEvent,
    TraceEventKind,
    TraceEvidenceManifest,
    normalize_observable_trace,
)
from agentpilot.search.trace_store import TraceStore

# Families own one basis axis each, their realizations rotate into a paired
# axis, and every family leans on a shared axis by a different amount.  That
# lean is what keeps two families from being exactly equidistant from a third
# direction, which the semantic gate would reject as an ambiguous tie.
FAMILY_COUNT = 32
_REALIZATION_AXIS = FAMILY_COUNT
_SHARED_AXIS = 2 * FAMILY_COUNT
EMBEDDING_DIMENSION = _SHARED_AXIS + 1
REALIZATION_STEP_DEGREES = 22.0
REALIZATION_COUNT = 3
_SHARED_LEAN_STEP = 0.02
_DISCOVER_FAMILIES_PER_BATCH = 4

# The loader re-derives both thresholds as the midpoint of the strict class
# gaps in the calibration split and requires an exact match, so these carry the
# float value that derivation actually produces rather than the rounded one.
TAU_REPEAT = 0.05499999999999999
TAU_ARM = 0.29999999999999993
BOUNDARY_MARGIN = 0.005

_ROOT_AVAILABLE_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)
_COMPATIBILITY = CompatibilityKey(
    "example-task-accuracy",
    "paired-example-evaluator",
    "judge_success",
    "maximize",
    "synthetic-example-environment",
)
_DESCRIPTOR = StateDescriptor({"task": {"kind": "arithmetic-solver"}})

_CELL_IDS = tuple(f"example-case-{index:02d}" for index in range(1, 17))

# The root program the fixture run inherits: a low but non-zero baseline, so the
# curve has somewhere to climb from.
_ROOT_PASS_RATE = 0.1875


class _SyntheticEvaluationPlanFreezer:
    """Freeze the public fixture's exact census before its actions run."""

    freezer_id = stable_id(
        "synthetic-example-evaluation-plan-freezer",
        _CELL_IDS,
        UNIFORM_ESTIMATOR_CONTRACT,
    )
    source_scope_id = "synthetic-example-action-source"

    def freeze_batch(
        self, *, state: SearchState, actions: tuple[PolicyAction, ...]
    ) -> FrozenEvaluationBatch:
        prior = FrozenInformationPriorSnapshot.create(
            run_id=state.run_id,
            batch_id=state.batch_id,
            state_id=state.state_id,
            cutoff=state.cutoff,
            compatibility=state.compatibility,
            source_scope_id=self.source_scope_id,
            task_counts={},
            task_weights={cell_id: 1.0 for cell_id in _CELL_IDS},
            weighting_contract="synthetic-uniform-census",
        )
        plans = []
        for action in actions:
            plan = FrozenPairedPlan(
                plan_id=stable_id(
                    "synthetic-example-paired-plan",
                    action.action_id,
                    _CELL_IDS,
                ),
                tier=EvidenceTier.ADAPTIVE_B,
                cells=tuple(
                    EvaluationCell(
                        cell_id=cell_id,
                        task_id=cell_id,
                        block_id=cell_id,
                        measurement_seed=index,
                    )
                    for index, cell_id in enumerate(_CELL_IDS)
                ),
                target_population_size=len(_CELL_IDS),
            )
            plans.append(
                FrozenActionEvaluationPlan.create(
                    run_id=state.run_id,
                    batch_id=state.batch_id,
                    state_id=state.state_id,
                    action_id=action.action_id,
                    cutoff=state.cutoff,
                    plan=plan,
                    sampling_draw=None,
                    prior_snapshot_id=prior.snapshot_id,
                    estimator_contract=UNIFORM_ESTIMATOR_CONTRACT,
                )
            )
        return FrozenEvaluationBatch(
            prior_snapshot=prior,
            action_plans=tuple(plans),
        )


_FAMILY_MECHANISMS = (
    "the solver commits to the first parsed candidate answer",
    "modular arithmetic shortcuts drop a sign on negative residues",
    "the retry loop reuses the exhausted reasoning context",
    "geometric constructions are not verified against the stated constraints",
    "large integer arithmetic overflows the intermediate representation",
    "the answer extractor accepts prose instead of a bare integer",
    "casework enumeration terminates before the final branch",
    "the verifier accepts an unsimplified radical form",
)
_FAMILY_DIRECTIONS = (
    "score every candidate before committing to one",
    "normalize residues into a canonical non-negative range",
    "reset the reasoning context between retries",
    "check each construction against the stated constraints",
    "carry intermediate products in exact arithmetic",
    "require the extractor to emit a bare integer",
    "enumerate every branch before terminating casework",
    "simplify radicals before the verifier compares them",
)
_REALIZATION_NOTES = (
    "apply it at the top of the solve entry point",
    "apply it inside the candidate reduction helper",
    "apply it as a post-condition on the returned answer",
)


def _family_of(text: str) -> int | None:
    marker = "[M"
    start = text.find(marker)
    if start < 0:
        return None
    end = text.find("]", start)
    if end < 0:
        return None
    try:
        return int(text[start + len(marker) : end])
    except ValueError:
        return None


def _realization_of(text: str) -> int:
    marker = "[R"
    start = text.find(marker)
    if start < 0:
        return 0
    end = text.find("]", start)
    if end < 0:
        return 0
    try:
        return int(text[start + len(marker) : end])
    except ValueError:
        return 0


def _unit_vector(family: int, realization: int) -> tuple[float, ...]:
    """Place a direction on its family axis, rotated by its realization.

    Same-family realizations stay between ``tau_repeat`` and ``tau_arm`` so they
    join one arm; different families stay far outside ``tau_arm`` so they open a
    new one, while the per-family lean on the shared axis keeps every pairwise
    distance distinct.
    """

    axis = family % FAMILY_COUNT
    lean = _SHARED_LEAN_STEP * (axis + 1)
    scale = 1.0 / math.hypot(1.0, lean)
    angle = math.radians(REALIZATION_STEP_DEGREES * (realization % REALIZATION_COUNT))
    values = [0.0] * EMBEDDING_DIMENSION
    values[axis] = math.cos(angle) * scale
    values[_SHARED_AXIS] = math.cos(angle) * lean * scale
    values[_REALIZATION_AXIS + axis] = math.sin(angle)
    return tuple(values)


def _batch_ordinal(batch_id: str) -> int:
    digits = "".join(character for character in batch_id if character.isdigit())
    return int(digits) if digits else 0


def _stable_fraction(*parts: str) -> float:
    return random.Random("|".join(parts)).random()


def _stable_slug(*parts: str) -> str:
    return f"{random.Random('|'.join(parts)).getrandbits(32):08x}"


def _semantic_input(name: str) -> str:
    return canonical_json(
        {
            "failure_mechanism": f"labelled failure mechanism {name}",
            "implementation_direction": f"labelled implementation direction {name}",
            "resolved_scope": ["solver.py"],
            "new_paths": [],
        }
    )


def _semantic_case(prefix: str, label: str, distance: float) -> dict:
    similarity = 1.0 - distance
    anchor = [0.0] * EMBEDDING_DIMENSION
    anchor[0] = 1.0
    candidate = [0.0] * EMBEDDING_DIMENSION
    candidate[0] = similarity
    candidate[1] = math.sqrt(max(0.0, 1.0 - similarity * similarity))
    return {
        "case_id": prefix,
        "group_id": f"group-{prefix}",
        "label": label,
        "annotation": f"adjudicated {prefix}",
        "anchor": {
            "item_id": f"{prefix}-anchor",
            "canonical_input": _semantic_input(f"{prefix}-anchor"),
            "embedding": anchor,
        },
        "candidate": {
            "item_id": f"{prefix}-candidate",
            "canonical_input": _semantic_input(f"{prefix}-candidate"),
            "embedding": candidate,
        },
    }


def _semantic_manifest(split: str, manifest_id: str, prefix: str) -> dict:
    return {
        "manifest_id": manifest_id,
        "split": split,
        "provenance": "target_model_labeled",
        "embedding_identity": {
            "encoder_id": "synthetic-example:embedding",
            "encoder_identity": "synthetic-example",
            "dimension": EMBEDDING_DIMENSION,
            "distance": "cosine",
        },
        "thresholds": {
            "threshold_id": "synthetic-example-thresholds",
            "tau_repeat": TAU_REPEAT,
            "tau_arm": TAU_ARM,
            "threshold_boundary_margin": BOUNDARY_MARGIN,
            "nearest_arm_tie_tolerance": 1e-12,
        },
        "requirements": {"minimum_cases_per_label": 1},
        "annotators": ["reviewer-a", "reviewer-b"],
        "minimum_annotators": 2,
        "adjudication_policy": "independent-labels-then-adjudicate",
        "embedding_source": f"precomputed dev fixture {prefix}",
        "threshold_selection_protocol": "strict-separated-gaps",
        "selection_fixture_id": (
            "synthetic-example-selection" if split == "acceptance" else None
        ),
        "cases": [
            _semantic_case(f"{prefix}-repeat", "repeat_paraphrase", 0.01),
            _semantic_case(f"{prefix}-same", "same_arm_novel_realization", 0.1),
            _semantic_case(f"{prefix}-distinct", "distinct_mechanism", 0.5),
        ],
    }


def _semantic_bundle():
    return parse_semantic_calibration_bundle(
        {
            "bundle_id": "synthetic-example-semantic-bundle",
            "selection": _semantic_manifest(
                "calibration", "synthetic-example-selection", "selection"
            ),
            "acceptance": _semantic_manifest(
                "acceptance", "synthetic-example-acceptance", "acceptance"
            ),
        }
    )


class _Completion:
    """Answer proposer, ranker and summarizer calls from the prompt alone."""

    provider_name = "synthetic-example"

    def __init__(self) -> None:
        self.calls = 0
        self.discovered = 0
        self.proposed = 0

    @staticmethod
    def _ranking(prompt: str) -> str:
        request_data = json.loads(prompt.splitlines()[-1])
        candidates = request_data["candidates"]
        return json.dumps({"ranking": [item["index"] for item in candidates]})

    @staticmethod
    def _lessons(prompt: str) -> str:
        """Produce small reusable lessons from the supplied batch facts."""

        request_data = json.loads(prompt.splitlines()[-1])
        actions = request_data.get("completed_actions") or []
        lessons = []
        for action in actions[:2]:
            targets = action["realization"]["change_target_indexes"]
            if not targets:
                continue
            lessons.append(
                {
                    "kind": "conditional_guidance",
                    "applies_when": (
                        "A later attempt starts from a similar parent program"
                    ),
                    "mechanism": action["hypothesis"]["failure_mechanism_hypothesis"],
                    "recommended_change": action["hypothesis"]["implementation_direction"],
                    "target_indexes": targets[:1],
                }
            )
        if not lessons:
            prior = request_data.get("prior_lessons") or []
            for lesson in prior[:1]:
                lessons.append(
                    {
                        "kind": lesson["kind"],
                        "applies_when": lesson["applies_when"],
                        "mechanism": lesson["mechanism"],
                        "recommended_change": lesson["recommended_change"],
                        "target_indexes": lesson["target_indexes"][:1],
                    }
                )
        return json.dumps({"lessons": lessons})

    def _direction(self, request) -> str:
        # The trace-aware proposer appends evidence and a turn boundary after
        # the request JSON. Read the named packets, not the last prompt line.
        request_data = next(
            json.loads(line)
            for line in request.prompt.splitlines()
            if line.startswith('{"allowed_creation_roots":')
        )
        trace_bundle = json.JSONDecoder().raw_decode(
            request.prompt.split("# Frozen trace evidence floor\n\n", 1)[1]
        )[0]
        guidance = request_data.get("untrusted_policy_guidance") or ""
        repairing = bool(request_data.get("previous_validation_error"))
        intent = request_data.get("proposal_intent") or ""
        source_family = _family_of(guidance)
        if "new concrete realization" in intent and source_family is not None:
            # An existing-arm slot must stay inside tau_arm, so keep the family
            # and move to another realization of it.
            family = source_family
            step = 2 if repairing else 1
            realization = (_realization_of(guidance) + step) % REALIZATION_COUNT
        elif "materially new" in intent:
            # A discover slot must stay unmatched, so hand it a family this run
            # has not opened yet.  Batches claim disjoint family blocks and each
            # discover inside a batch takes the next one.
            family = (
                _batch_ordinal(request.metadata.get("batch_id", ""))
                * _DISCOVER_FAMILIES_PER_BATCH
                + self.discovered
            ) % FAMILY_COUNT
            self.discovered += 1
            realization = 0
        else:
            # Memory has no slots and dedups a slate on content alone, so every
            # call in a slate has to differ.
            family = self.proposed % FAMILY_COUNT
            realization = (self.proposed // FAMILY_COUNT) % REALIZATION_COUNT
            self.proposed += 1
        target = next(
            item["ref"] for item in request_data["repository_catalog"]
            if item["kind"] == "file" and item["path"] == "src/solver.py"
        )
        evidence = [trace_bundle["initial_spans"][0]["evidence_ref"]]
        text = family % len(_FAMILY_MECHANISMS)
        payload = {
            "observed_behavior": "The synthetic solver returns an incorrect answer on the cited case.",
            "failure_mechanism": f"[M{family}] {_FAMILY_MECHANISMS[text]}",
            "implementation_direction": (
                f"[M{family}] [R{realization}] {_FAMILY_DIRECTIONS[text]}; "
                f"{_REALIZATION_NOTES[realization]}"
            ),
            "scope_refs": [target],
            "new_paths": [],
            "evidence_refs": evidence,
        }
        if request_data.get("fixed_d2_arm_spec") is not None:
            payload["failure_mechanism"] = request_data["fixed_d2_arm_spec"]["failure_mechanism"]
        return json.dumps({"kind": "submit", "direction": payload})

    def complete(self, request):
        self.calls += 1
        if request.system.startswith("Rank only"):
            text = self._ranking(request.prompt)
        elif request.system.startswith(("Rewrite reusable", "Rewrite the reusable")):
            text = self._lessons(request.prompt)
        else:
            text = self._direction(request)
        return ProviderReceipt(
            provider=self.provider_name,
            model=request.model,
            attestation_id=f"offline-completion-{self.calls}",
            text=text,
            input_tokens=1_200,
            output_tokens=280,
            cost_usd=0.0042,
            accounting_status=AccountingStatus.PROVIDER_REPORTED,
        )


class _Embedding:
    """Map a canonical direction onto its family/realization basis vector."""

    provider_name = "synthetic-example"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, request):
        self.calls += 1
        vectors = []
        for text in request.texts:
            family = _family_of(text)
            realization = _realization_of(text)
            vectors.append(_unit_vector(0 if family is None else family, realization))
        return EmbeddingResult(
            vectors=tuple(vectors),
            provider=self.provider_name,
            model=request.model,
            usage=ProviderUsage(
                input_tokens=64,
                output_tokens=0,
                cost_usd=0.00002,
                accounting_status=AccountingStatus.PROVIDER_REPORTED,
                provider_request_id=f"offline-embedding-{self.calls}",
            ),
        )


_GIT_IDENTITY = (
    "-c",
    "user.name=AgentPilot dev fixture",
    "-c",
    "user.email=fixture@example.invalid",
)
_FIXTURE_DATE = "2000-01-01T00:00:00+00:00"
_VALUE_TRAILER = "fixture-value:"

_SOLVER_SEED = '''"""Reference arithmetic solver the fixture run evolves."""


def solve(problem: str) -> int:
    """Return the integer answer for one competition problem."""
    for candidate in reduce_candidates(problem):
        if candidate is not None:
            return candidate
    raise ValueError("no candidate answer survived reduction")


def reduce_candidates(problem: str) -> list[int]:
    """Enumerate answer candidates in the order the solver proposes them."""
    return [extract_answer(step) for step in problem.splitlines() if step.strip()]
'''

_EXTRACT_SEED = '''"""Answer extraction."""


def extract_answer(text: str) -> int | None:
    """Read a bare integer out of one reasoning step."""
    stripped = text.strip().rstrip(".")
    return int(stripped) if stripped.lstrip("-").isdigit() else None
'''

_ARITHMETIC_SEED = '''"""Integer helpers used by the solver."""


def normalize_residue(value: int, modulus: int) -> int:
    """Map a residue into ``[0, modulus)``."""
    return value % modulus


def exact_product(values: list[int]) -> int:
    """Multiply without intermediate floating point."""
    total = 1
    for value in values:
        total *= value
    return total
'''


class _CandidateRepository:
    """A real git repository the fixture's candidates commit into.

    The inspector reads patches straight from the repository, so a fixture whose
    commits are invented strings can never exercise that view.  Children are
    built with ``commit-tree`` against an isolated index rather than a checkout,
    which keeps concurrent workers from fighting over a working tree.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self.root_sha = self._ensure()

    def _git(
        self, *args: str, stdin: bytes | None = None, index: str | None = None,
        strip: bool = True,
    ) -> str:
        env = {"GIT_INDEX_FILE": index} if index else None
        process = subprocess.run(
            ["git", "-C", str(self.root), *_GIT_IDENTITY, *args],
            capture_output=True,
            input=stdin,
            env={**_os_environ(), **(env or {})},
            check=True,
        )
        output = process.stdout.decode()
        return output.strip() if strip else output

    def _ensure(self) -> str:
        if (self.root / ".git").is_dir():
            return self._git("rev-parse", "HEAD")
        self.root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            capture_output=True,
        )
        source = self.root / "src"
        source.mkdir(exist_ok=True)
        (source / "solver.py").write_text(_SOLVER_SEED)
        (source / "extract.py").write_text(_EXTRACT_SEED)
        (source / "arithmetic.py").write_text(_ARITHMETIC_SEED)
        self._git("add", "-A")
        self._git(
            "commit",
            "-q",
            "-m",
            "seed the reference solver the fixture run evolves",
            f"--date={_FIXTURE_DATE}",
        )
        return self._git("rev-parse", "HEAD")

    def read(self, sha: str, path: str) -> str:
        return self._git("show", f"{sha}:{path}", strip=False)

    def repository_map(self, parent) -> ParentRepositoryMap:
        candidate = parent.candidate
        files = []
        for path in ("src/arithmetic.py", "src/extract.py", "src/solver.py"):
            source = self.read(candidate.commit_sha, path)
            symbols = tuple(sorted((
                RepositorySymbol(node.name, "function", node.lineno, node.end_lineno)
                for node in ast.parse(source).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ), key=lambda item: item.name))
            files.append(RepositoryFileMap(path, symbols, len(source.encode("utf-8"))))
        return ParentRepositoryMap.create(
            parent_id=candidate.parent_id,
            commit_sha=candidate.commit_sha,
            tree_sha=self._git("rev-parse", f"{candidate.commit_sha}^{{tree}}"),
            editable_roots=("src",),
            files=tuple(files),
        )

    def read_definition(self, *, request, target, offset, count) -> ReadResult:
        lines = self.read(request.parent_selection.commit_sha, target.file).splitlines()
        first, last = target.lines
        definition = lines[first - 1:last]
        end = min(offset + count, len(definition))
        return ReadResult(target.ref, "target", offset, end, len(definition), "\n".join(definition[offset:end]))

    def message(self, sha: str) -> str:
        return self._git("show", "-s", "--format=%B", sha)

    def commit_child(
        self, parent_sha: str, path: str, content: str, message: str
    ) -> str:
        """Commit one edited file on top of ``parent_sha`` and return its sha."""

        with self._lock:
            blob = self._git("hash-object", "-w", "--stdin", stdin=content.encode())
            with tempfile.NamedTemporaryFile(prefix="fixture-index-") as index_file:
                index = index_file.name
                self._git("read-tree", parent_sha, index=index)
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{path}",
                    index=index,
                )
                tree = self._git("write-tree", index=index)
            sha = self._git("commit-tree", tree, "-p", parent_sha, "-m", message)
            # commit-tree leaves the commit unreferenced, which makes it garbage
            # collectable; the inspector needs to read it back later
            self._git("update-ref", f"refs/candidates/{sha}", sha)
            return sha


def _os_environ() -> dict[str, str]:
    import os

    # commit ids must not drift between fixture rebuilds
    return {
        **os.environ,
        "GIT_AUTHOR_DATE": _FIXTURE_DATE,
        "GIT_COMMITTER_DATE": _FIXTURE_DATE,
        "GIT_AUTHOR_NAME": "AgentPilot dev fixture",
        "GIT_AUTHOR_EMAIL": "synthetic-example@agentpilot.invalid",
        "GIT_COMMITTER_NAME": "AgentPilot dev fixture",
        "GIT_COMMITTER_EMAIL": "synthetic-example@agentpilot.invalid",
    }


class _Runner:
    """Return a deterministic outcome that varies across every fate."""

    def __init__(
        self,
        lease_manager,
        repository: _CandidateRepository,
        unit_of_work,
        trace_store: TraceStore,
    ) -> None:
        self.lease_manager = lease_manager
        self.repository = repository
        self.unit_of_work = unit_of_work
        self.trace_store = trace_store
        self.results: dict[str, ExecutionResult] = {}
        # commit sha -> measured pass rate, so a child is scored relative to the
        # program it was actually derived from and the lineage carries value
        self.value_by_commit: dict[str, float] = {}

    @staticmethod
    def _fate(action_id: str) -> str:
        draw = _stable_fraction("fate", action_id)
        if draw < 0.44:
            return "improved"
        if draw < 0.70:
            return "regressed"
        if draw < 0.80:
            return "deviated"
        if draw < 0.90:
            return "inconclusive"
        return "unattributable"

    def _parent_value(self, action) -> float:
        """Recover the parent's measured value.

        Each batch runs in a fresh process, so the in-memory table only covers
        this batch; the value is also stamped into the commit message, which is
        what keeps a lineage's scores continuous across batches.
        """
        sha = getattr(action, "parent_commit_sha", None)
        if sha is None:
            return _ROOT_PASS_RATE
        cached = self.value_by_commit.get(sha)
        if cached is not None:
            return cached
        for line in self.repository.message(sha).splitlines():
            if line.startswith(_VALUE_TRAILER):
                return float(line[len(_VALUE_TRAILER) :].strip())
        return _ROOT_PASS_RATE

    @staticmethod
    def _cells_for(action_id: str, target_passes: int) -> dict[str, bool]:
        """Pick which cells pass, keeping the count equal to the target.

        Cells are ordered by a per-action hash, so two programs with the same
        pass rate still disagree about *which* problems they solve.
        """
        ranked = sorted(
            _CELL_IDS,
            key=lambda cell: _stable_fraction("cell", action_id, cell),
        )
        winners = set(ranked[:target_passes])
        return {cell: cell in winners for cell in _CELL_IDS}

    def _commit(self, action, fate: str, value: float) -> str:
        """Write the edit this action's direction describes and commit it.

        A guard is appended to the solver, so the patch reads as a real attempt
        at the failure mechanism the direction names and the diff view has
        something faithful to show.
        """
        parent_sha = action.parent_commit_sha or self.repository.root_sha
        direction = action.direction
        mechanism = getattr(direction, "failure_mechanism", "unspecified mechanism")
        approach = getattr(direction, "implementation_direction", "unspecified edit")
        slug = _stable_slug(action.action_id)
        source = self.repository.read(parent_sha, "src/solver.py")
        guard = (
            f"\n\ndef guard_{slug}(candidate: int | None) -> int | None:\n"
            f'    """Guard against: {mechanism}.\n\n'
            f"    {approach}\n"
            f'    """\n'
            f"    if candidate is None:\n"
            f"        return None\n"
            f"    return candidate\n"
        )
        edited = source.rstrip("\n") + guard
        return self.repository.commit_child(
            parent_sha,
            "src/solver.py",
            edited,
            f"{mechanism}\n\n{approach}\n\nfixture-fate: {fate}\n{_VALUE_TRAILER} {value}",
        )

    def _outcome(self, action, completed) -> tuple[WorkerResult, PairedOutcome]:
        fate = self._fate(action.action_id)
        magnitude = 0.02 + 0.16 * _stable_fraction("size", action.action_id)
        statistic = magnitude if fate == "improved" else -magnitude
        # a paired estimate is noisier than the point delta it brackets
        standard_error = 0.01 + 0.05 * _stable_fraction("se", action.action_id)
        adherence = (
            ImplementationAdherence.DEVIATED
            if fate == "deviated"
            else ImplementationAdherence.FOLLOWED
        )
        validity = (
            EvaluationValidity.INCONCLUSIVE
            if fate == "inconclusive"
            else EvaluationValidity.VALID
        )
        attribution = (
            OutcomeAttribution.NON_ATTRIBUTABLE
            if fate == "unattributable"
            else OutcomeAttribution.ATTRIBUTABLE
        )
        worker_status = (
            WorkerExecutionStatus.COMPLETED
            if fate != "deviated"
            else WorkerExecutionStatus.COMPLETED
        )
        # Score the child against its parent rather than in isolation: an
        # improvement lifts the branch it came from, a regression drags it down,
        # which is what makes the lineage readable as evolution.
        parent_value = self._parent_value(action)
        target = min(1.0, max(0.0, parent_value + statistic))
        passed = max(0, min(len(_CELL_IDS), round(target * len(_CELL_IDS))))
        cell_results = self._cells_for(action.action_id, passed)
        absolute = round(passed / len(_CELL_IDS), 4)
        child_sha = self._commit(action, fate, absolute)
        self.value_by_commit[child_sha] = absolute
        _publish_traces(
            self.trace_store, self.unit_of_work, self.repository,
            program_id=child_sha, commit_sha=child_sha,
            cell_results=cell_results, available_at=completed,
        )
        worker = WorkerResult(
            worker_result_id=f"worker:{action.action_id}",
            action_id=action.action_id,
            status=worker_status,
            adherence=adherence,
            started_at=completed,
            completed_at=completed,
            result_commit_sha=child_sha,
            execution_attestation=None,
        )
        with self.unit_of_work() as work:
            evaluation_plan = work.evaluation_plans.plan_for_action(action.action_id)
        if evaluation_plan is None:
            raise RuntimeError("synthetic action has no frozen evaluation plan")
        outcome = PairedOutcome(
            outcome_id=f"outcome:{action.action_id}",
            action_id=action.action_id,
            evidence_tier=EvidenceTier.ADAPTIVE_B,
            evaluation_plan_record_id=evaluation_plan.record_id,
            validity=validity,
            attribution=attribution,
            signed_effectiveness_statistic=round(statistic, 4),
            constraints_pass=fate != "regressed" or magnitude < 0.1,
            completed_at=completed,
            primary_delta_mean=round(absolute - parent_value, 4),
            primary_delta_lower=round(statistic - 1.96 * standard_error, 4),
            admission_fate="admitted" if fate == "improved" else "rejected",
            child_readout=ChildProgramReadout(
                child_program_id=child_sha,
                descriptor=_DESCRIPTOR,
                absolute_primary_value=absolute,
                cell_results=cell_results,
            ),
        )
        return worker, outcome

    def execute(self, action, fence):
        if self.lease_manager is None:
            raise RuntimeError("dev fixture runner has no lease manager")
        mutation_checked_at = utc_now()
        assert_action_execution_fence_live(fence, self.lease_manager)
        existing = self.results.get(action.action_id)
        if existing is not None:
            verified_at = utc_now()
            assert_action_execution_fence_live(fence, self.lease_manager)
            original = existing.worker.execution_attestation
            if original is None:
                raise RuntimeError("stored action result lacks attestation")
            if original.fence_id == fence.fence_id:
                return existing
            replay = replace(
                original,
                fence_id=fence.fence_id,
                runner_id=fence.runner_id,
                mode=ActionExecutionMode.REPLAYED,
                original_fence_id=(original.original_fence_id or original.fence_id),
                replay_verified_at=verified_at,
            )
            return replace(
                existing,
                worker=replace(existing.worker, execution_attestation=replay),
            )
        completed = utc_now()
        assert_action_execution_fence_live(fence, self.lease_manager)
        worker, outcome = self._outcome(action, completed)
        worker = replace(
            worker,
            execution_attestation=ActionExecutionAttestation(
                fence_id=fence.fence_id,
                runner_id=fence.runner_id,
                worktree_id=f"worktree:{action.action_id}",
                parent_commit_sha=fence.parent_commit_sha,
                result_commit_sha=worker.result_commit_sha,
                mutation_fence_checked_at=mutation_checked_at,
                commit_fence_checked_at=completed,
            ),
        )
        result = ExecutionResult(worker, outcome)
        self.results[action.action_id] = result
        return result


def _publish_traces(
    store, unit_of_work, repository, *, program_id, commit_sha,
    cell_results, available_at,
) -> None:
    """Publish explicitly synthetic Visible-A evidence for each candidate.

    Every process rebuilds the same root manifest, while child manifests are
    persisted with the candidate so subsequent batches can propose from it.
    These fixtures are protocol examples, not real benchmark measurements.
    """
    tree_sha = repository._git("rev-parse", f"{commit_sha}^{{tree}}")
    # A worker can stop after publishing traces but before the batch consumes
    # its result. Replaying that deterministic candidate must retain the first
    # capture time because occurrence IDs deliberately exclude timestamps.
    with unit_of_work() as work:
        previous = work.trace_evidence.list_captures_for_program(program_id)
    capture_times = {
        item.artifact_ref.available_at for item in previous
        if item.artifact_ref is not None
        and item.artifact_ref.commit_sha == commit_sha
        and item.artifact_ref.tree_sha == tree_sha
    }
    if len(capture_times) > 1:
        raise ValueError("synthetic candidate has inconsistent trace capture times")
    if capture_times:
        available_at = next(iter(capture_times))
    artifacts, cases, captures = [], [], []
    for cell_id, passed in sorted(cell_results.items()):
        trace = normalize_observable_trace(
            trace_contract="synthetic-example-v1",
            events=(
                TraceEvent(0, TraceEventKind.TOOL_RESULT, "solver", "solve",
                           f"Synthetic case {cell_id}: returned {'correct' if passed else 'incorrect'} answer.", None),
                TraceEvent(1, TraceEventKind.FINAL, "evaluator", None,
                           "pass" if passed else "fail", None),
            ),
        )
        artifact = TraceArtifactRef.create(
            coordinates=TraceCaptureCoordinates(
                program_id, commit_sha, tree_sha, cell_id, cell_id, 0, available_at,
            ),
            format="normalized-json", byte_count=len(trace.canonical_bytes()),
            event_count=len(trace.events), trace_contract=trace.trace_contract,
        )
        store.put(artifact.trace_ref_id, trace)
        artifacts.append(artifact)
        captures.append(TraceCaptureResult(
            record=TraceCaptureRecord(
                "synthetic-example", program_id, cell_id, TraceCaptureStatus.COMPLETE,
                artifact.trace_ref_id, None,
            ),
            artifact_ref=artifact,
        ))
        cases.append(TraceCaseIndex.create(
            case_key=f"{program_id}:{cell_id}", trace_ref_id=artifact.trace_ref_id,
            outcome="pass" if passed else "fail", measurement_status="valid",
            trusted_failure_label=None if passed else "synthetic_wrong_answer",
            final_artifact_state="answer_present",
        ))
    manifest = TraceEvidenceManifest.create(
        program_id=program_id, commit_sha=commit_sha, tree_sha=tree_sha,
        compatibility=_COMPATIBILITY, task_set_id="synthetic-example-cases",
        measurement_profile_id="synthetic-example-visible-a",
        artifact_refs=tuple(artifacts), case_indexes=tuple(cases),
        available_at=available_at,
    )
    store.register_manifest(manifest)
    with unit_of_work() as work:
        for capture in captures:
            work.trace_evidence.add_capture(capture)
        work.trace_evidence.add_manifest(manifest)


def create_dependencies(config) -> AssemblyDependencies:
    """Build the deterministic synthetic adapter in every fresh process."""

    completion = _Completion()
    embedding = _Embedding()
    if config.environment.settings.get("root_seed") != "explicit":
        raise ValueError("dev fixture adapter requires an explicit root_seed")

    repository = _CandidateRepository(Path(config.runtime.runs_root) / "candidate-repo")

    def environment(context: BatchAssemblyContext) -> Environment:
        trace_store = TraceStore(
            Path(config.runtime.runs_root) / stable_id("example-traces", context.run_id)
        )
        root_cells = {
            cell: index < round(_ROOT_PASS_RATE * len(_CELL_IDS))
            for index, cell in enumerate(_CELL_IDS)
        }
        CanonicalSearchReadModelWriter(context.unit_of_work).seed_root(
            run_id=context.run_id,
            candidate=ParentCandidate(
                parent_id="root",
                commit_sha=repository.root_sha,
                descriptor=_DESCRIPTOR,
                primary_value=_ROOT_PASS_RATE,
            ),
            compatibility=_COMPATIBILITY,
            available_at=_ROOT_AVAILABLE_AT,
            cell_results=root_cells,
        )
        _publish_traces(
            trace_store, context.unit_of_work, repository,
            program_id="root", commit_sha=repository.root_sha,
            cell_results=root_cells, available_at=_ROOT_AVAILABLE_AT,
        )
        common = {
            "search_inputs": DurableSearchInputSource(
                unit_of_work=context.unit_of_work,
                compatibility=_COMPATIBILITY,
                parent_descriptor=_DESCRIPTOR,
                direction_descriptor=_DESCRIPTOR,
                budget={"rounds": 3},
                carrier_context=(
                    "Edit only files under src/. Keep the public solve() signature."
                ),
                repository_map_builder=repository.repository_map,
                require_visible_a_trace=True,
            ),
            "action_runner": ActionRunnerBinding(
                runner_id="synthetic-example-runner",
                runner=_Runner(
                    context.lease_manager,
                    repository,
                    context.unit_of_work,
                    trace_store,
                ),
                lease=context.lease,
                lease_manager=context.lease_manager,
                scope_identity="synthetic-example-no-mutation",
            ),
            "evaluation_plan_freezer": _SyntheticEvaluationPlanFreezer(),
            "effectiveness_rule": EffectivenessRule(0.0, "point"),
            "trace_store": trace_store,
            "definition_reader": repository,
        }
        if config.policy.method.value == "memory":
            return Environment(**common)
        return Environment(
            **common,
            parent_neighborhood=NamedNearStatePredicate(
                "parent-blocks", lambda observed, query, level: True
            ),
            direction_neighborhood=NamedNearStatePredicate(
                "direction-blocks", lambda observed, query, level: True
            ),
            applicability_builder=NamedApplicabilityBuilder(
                "parent-task-class",
                lambda state, parent: DirectionApplicability(
                    parent.parent_id,
                    "arithmetic-solver",
                ),
            ),
        )

    return AssemblyDependencies(
        providers=ProviderRegistry(
            completions={"synthetic-example": completion},
            embeddings={"synthetic-example": embedding},
        ),
        environment_factory=environment,
        semantic_calibration_bundle=(
            _semantic_bundle() if config.policy.method.value == "ucb" else None
        ),
    )


__all__ = ["create_dependencies"]
