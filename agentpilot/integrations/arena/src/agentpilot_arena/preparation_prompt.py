"""Preparation instructions for finding an initial program and evaluation."""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path


def build_preparation_prompt(
    *, goal: str, program: Path | None, evaluation: Path | None
) -> str:
    request = json.dumps(
        {
            "goal": goal,
            "existing_program": str(program) if program is not None else None,
            "existing_evaluation": str(evaluation) if evaluation is not None else None,
        },
        ensure_ascii=False,
        indent=2,
    )
    core = resources.files("agentpilot")
    integration = resources.files("agentpilot_arena")
    return f"""# Prepare an AgentPilot starting program and evaluation

Prepare the missing initial program and/or evaluation for the user's task using
an existing implemented AgentPilot adapter. This is preparation for the real
proposer/worker/evaluation runner, not a substitute improvement loop.

## User request

{request}

## Work to perform

1. Inspect any supplied program and evaluation first. Reuse compatible existing
   inputs. Treat their locations as read-only: if adaptation is needed, make a
   separate copy in this workspace. Fill only what is missing. Do not substitute
   an unrelated benchmark just because an integration already exists.
2. Find suitable existing repositories, datasets, task definitions, and graders.
   Use the harness's available search/network tools and record actual sources
   and revisions. Prefer a small runnable baseline and an established evaluation
   matching the user's goal. Prepare suitable inputs and checks for an existing
   adapter; do not build a generic adapter or fabricate an unrelated benchmark
   to make the handoff pass. Report a missing adapter as a blocked prerequisite.
3. Place new files and isolated dependency environments under this workspace.
   Keep the editable program separate from evaluation code, tasks, and answers.
   Pin downloaded repositories and dataset versions. The handoff requires a clean
   Git program revision. Reuse a suitable existing revision while preserving the
   user's edits and commit restrictions. Do not create a commit, even in a copy,
   to bypass a prohibition; report this prerequisite if it cannot be met.
   Do not change global harness configuration.
4. Run a small smoke check with visible development inputs when dependencies and
   credentials permit. Record the exact command, observed result, and log path in
   PREPARATION.md. Check meaningful cases, grader behavior, comparable inputs and
   correctness constraints. Performance goals need actual measurements and an
   appropriate repeat/noise policy; ordinary passing tests are insufficient.
   Distinguish missing infrastructure from a valid measurement of a wrong answer.
   Do not invent benchmark scores, passing tests, dataset hashes, or cost reports.
5. Write PREPARATION.md with the chosen program/evaluation, provenance, install
   commands, entrypoint, editable code paths, metric meaning, dataset splits,
   completed checks, remaining settings, and the runner command. Write
   prepared.json using exactly one of the handoff formats below. A final chat
   answer alone is not a handoff: the calling agent reads prepared.json from disk.

When acting as a standalone preparation worker, return this delivery to the
calling agent without launching a campaign. When acting as the current host for
the user's full AgentPilot request, validate the delivery and continue the actual
configured runner once evaluation, provider/worker, environment and budget
prerequisites are met within the existing authorization. Do not merely print the
next command or continue your own iterative code edits. Otherwise report
preparation incomplete and the specific missing prerequisites.

Verify current source/configuration/evaluator identities against actual smoke
evidence. Arena baseline status complete can include invalid captures: inspect
measurements and diagnostics before research. A valid zero score differs from
apparatus failure. Retain existing run identities and recovery records; do not
repeat paid work merely because billing remains pending. Unknown costs remain
unknown, not zero or the configured reservation.

Do not read or embed credential values in prompts, manifests, or logs. Reference
environment-variable names. Use the configured harness permissions for network
access; if source access is unavailable, report what remains missing.

## Route 1: existing Arena research setup

Use this route only for AIME 2024+2025 or Terminal-Bench 2.1 with a Harbor Terminus 2
program. Other Python projects do not become compatible by renaming a path.
The program checkout must contain src/harbor/agents/terminus_2. The trusted Harbor
checkout must be separate, clean, and have executable .venv/bin/python and
.venv/bin/harbor. Reuse the installed integration's documented formats:

- Setup and validation: {integration.joinpath('setup.py')}
- AIME task split: {integration.joinpath('aime', 'data', 'split.example.json')}
- TB2.1 freeze: {integration.joinpath('tb2', 'data', 'freeze.example.json')}
- TB2.1 preparation: {integration.joinpath('tb2', 'build_experiment.py')}

AIME tasks are bundled; the ordinary setup command prepares the task image.
For TB2.1 also supply the real task Git checkout and offline-verifier authority
produced by its preparation tools. Never fabricate an authority file. Docker
network selection, worker/model settings, and run budgets come from existing
configuration or the user's choices. Native worker login does not configure the
formal proposer/policy/task-model provider. Do not invent budgets or credentials.

The manifest for AIME is:

```json
{{
  "schema": "agentpilot-preparation/v1",
  "program": {{
    "path": "program",
    "revision": "ACTUAL_FULL_GIT_COMMIT_SHA",
    "entrypoint": "builtin:terminus-2"
  }},
  "evaluation": {{
    "kind": "arena_setup",
    "benchmark": "aime",
    "harbor": "evaluation/harbor"
  }},
  "sources": [
    {{"url": "ACTUAL_SOURCE_URL_OR_LOCAL_PATH", "revision": "ACTUAL_REVISION", "path": "program"}},
    {{"url": "ACTUAL_SOURCE_URL_OR_LOCAL_PATH", "revision": "ACTUAL_REVISION", "path": "evaluation/harbor"}}
  ]
}}
```

For TB2.1 set benchmark to "tb21" and add "tasks" and "verifier_authority" paths
inside evaluation. Do not add arbitrary command-line flags to the manifest.
The controller imports these paths through agentpilot-arena-setup --prepared.

## Route 2: an existing custom core adapter

Use an implemented core AgentPilot environment binding for the actual task. The
Arena research setup is specific to Harbor/AIME/TB2.1; changing a name in TOML
does not register a new benchmark. If no compatible binding exists, return a
blocked handoff. Developing a generic adapter is a separate task.

Check the existing adapter against these installed interfaces:

- {core.joinpath('app', 'bindings.py')}: AssemblyDependencies, ProviderRegistry,
  BatchAssemblyContext, Environment, ActionRunnerBinding.
- {core.joinpath('app', 'search_read_model.py')}: seed the real initial program
  and baseline with CanonicalSearchReadModelWriter.
- {core.joinpath('app', 'runtime_config.py')} and {core.joinpath('app', 'config.py')}:
  runtime TOML schema.

The installed factory must return AssemblyDependencies and supply real program
seeding, worker execution, frozen paired evaluation, feedback and trace bindings.
A BenchAdapter, standalone test command or synthetic example alone is insufficient.
Keep the adapter/evaluator outside editable code, and retain its development,
comparison and held-out protocol. Parent and candidate must use comparable
tasks, seeds and budgets. Unknown costs remain unknown, never a fabricated zero.

Record missing model, credential-name, budget or environment settings. Check that
the adapter actually uses the delivered program and evaluator, and that current
versions match the smoke evidence. Static validation does not prove this. For
an existing runtime, preserve its configuration and use its actual smoke/run
procedure rather than substituting a host-driven loop.

The core handoff uses:

```json
{{
  "schema": "agentpilot-preparation/v1",
  "program": {{
    "path": "program",
    "revision": "ACTUAL_FULL_GIT_COMMIT_SHA",
    "entrypoint": "your_program:run"
  }},
  "evaluation": {{
    "kind": "core_config",
    "config": "runtime.toml",
    "adapter": "evaluation"
  }},
  "sources": [
    {{"url": "ACTUAL_SOURCE_URL_OR_LOCAL_PATH", "revision": "ACTUAL_REVISION", "path": "program"}},
    {{"url": "ACTUAL_SOURCE_URL_OR_LOCAL_PATH", "revision": "ACTUAL_REVISION", "path": "evaluation"}}
  ]
}}
```

Paths are relative to prepared.json, or absolute for reused existing inputs.
Replace every example placeholder with a real identity. Include the origin of
newly written code as a local source and give its actual version/revision.

If the required artifacts cannot be prepared, write a blocked handoff instead:

```json
{{
  "schema": "agentpilot-preparation/v1",
  "status": "blocked",
  "missing": ["specific missing input or capability"],
  "reason": "What was attempted and why preparation could not finish"
}}
```

Preserve useful completed work and explain how to continue in PREPARATION.md.
"""
