# Prepare and continue the configured AgentPilot workflow

Keep the selected task, evaluator, worker/model settings and budget. Prefer the selected checkout's documentation and environment; the skill installer supplies the core, not every adapter, provider or benchmark dependency.

## Prepare missing inputs

Use the current agent to establish the objective from project evidence, find a suitable starting program and prepare a meaningful, comparable evaluation. Reuse implemented adapters. A dataset, test command or grading script alone does not supply a core environment binding. Generic adapter development is outside this preparation path; report the missing binding if no suitable one exists.

When a formal handoff is useful, generate the existing preparation prompt without another worker:

```text
uv run --project /checkout/agentpilot/integrations/arena --no-sync agentpilot-arena-prepare --goal "the actual task" --output /fresh/preparation
```

Add `--program` or `--eval` for existing inputs, then read `prompt.md` and prepare its files with the current host. Do not add `--execute` or create a worker configuration just for preparation. Check the delivery with `agentpilot-arena-prepare --check /fresh/preparation/prepared.json` in the same environment. The [preparation guide](https://github.com/HKUDS/AgentPilot/blob/main/agentpilot/integrations/arena/docs/preparation.md) describes the handoff routes.

Respect existing edits and commit restrictions. If a clean pinned program is required, use a suitable existing revision without silently discarding user changes. Do not create a commit to bypass a prohibition; report the unmet prerequisite instead.

## Establish readiness, then hand off

Before starting improvement, verify these conditions from the actual configuration and execution evidence:

- The objective matches the evaluation: meaningful cases and grader, comparable parent/candidate inputs, correctness constraints, and appropriate repeats for noisy measurements. Correctness tests alone do not establish a performance objective. Preserve the evaluator and existing development/comparison/held-out separation.
- The implemented adapter uses the intended program revision and evaluator. Check source/configuration/evaluation identities against the actual smoke or baseline results; changed inputs require fresh checks. Static TOML validation and prose claiming success are insufficient.
- Evaluation produced usable measurements and logs. A valid wrong answer or zero score can be a baseline; an apparatus failure cannot. Arena baseline `complete` may include invalid captures: inspect measurements and diagnostics, and resolve failures that leave the intended comparison unmeasurable.
- The required providers, worker, environment and authorized budget are available. Arena research can reuse the worker CLI's model/login for proposer and Memory policy calls when their API credentials are absent. The task solver and UCB embedding model still need their configured providers; custom core adapters own their provider assembly. Reuse existing settings and permissions. Ask only for a genuinely missing choice or authorization; do not invent budgets or copy paid-run example amounts.

When these conditions are met and execution is within the user's request, **continue by invoking the configured runner below**. Do not stop at printing a next command, or replace the runner with your own iterative edits. AgentPilot must select directions, dispatch the worker, evaluate candidates and consume feedback. Report its actual run identity and artifacts; a started run is not proof of improvement.

If a condition cannot be met, preserve preparation work and state what is missing. Do not label preparation as an RSI run or silently switch to a host-driven optimization loop. No extra readiness database or session protocol is required.

## Existing Arena integration

Read `agentpilot/integrations/arena/README.md` ([setup and run guide](https://github.com/HKUDS/AgentPilot/blob/main/agentpilot/integrations/arena/README.md)). This path supports Harbor/Terminus 2 on AIME or Terminal-Bench 2.1. In its installed environment, use the existing commands:

```text
agentpilot-arena-setup --prepared /preparation/prepared.json --output /run --worker-config /worker.toml --model MODEL --cell-max-cost-usd CELL_LIMIT
agentpilot-arena-baseline --config /run/runtime.toml --output /run/baseline --execute --budget-usd BASELINE_BUDGET --cell-allowance-usd CELL_ALLOWANCE --task-pool /run/task-pool
agentpilot-arena-research --config /run/runtime.toml --evidence /run/baseline/packet --output /run/search --execute --budget-usd RESEARCH_BUDGET --call-allowance-usd CALL_ALLOWANCE --cell-allowance-usd CELL_ALLOWANCE --task-pool /run/task-pool
```

Replace placeholders with existing or user-established choices; add the configured `--env-file` when needed. TB2.1 setup also needs its chosen `--docker-inner-address-pool`. A runnable existing runtime skips setup; usable matching baseline evidence skips recollection. The research route defaults to API credentials for proposer and policy roles, falling back to the configured worker CLI when a role's key is absent. Add `--proposer-access worker --policy-access worker` to explicitly reuse worker access, or select `api` to require API credentials. Task-model calls still use OpenRouter. Keep the selected access routes and worker profile unchanged on resume.

Resume baseline collection with its same output and arguments; resume research with its original configuration, evidence, output and `--resume`. Preserve persisted identities and interrupted-work receipts rather than starting duplicate work. Separate baseline and research budgets and per-call reservations are not hard caps on in-flight calls. Worker CLI charges, including reused proposer/policy calls, remain external with retained budget allowances; never report them as zero or substitute reservations for measured cost.

## Existing custom core adapter

Use `core_config` only for an implemented adapter and its documented installation/smoke procedure. See the [environment guide](https://github.com/HKUDS/AgentPilot/blob/main/agentpilot/docs/environment-adapters.md) and [configuration reference](https://github.com/HKUDS/AgentPilot/blob/main/agentpilot/configs/README.md). In the environment containing that adapter and its providers:

```text
agentpilot validate-config --config /path/runtime.toml
agentpilot run --config /path/runtime.toml --run-id RUN --batch-id BATCH --rounds N
agentpilot resume --config /path/runtime.toml --batch-id BATCH
```

The installed launcher in SKILL.md accepts these core commands when its environment contains the required packages. `validate-config` checks structure without importing the adapter. Verify the real adapter/evaluation before `run`; preserve configuration and batch identity for `resume`, and use fresh IDs only for a new run. Ordinary tests or a synthetic adapter cannot stand in for the requested evaluation.

If the supplied configuration belongs to the separate `rsi-arena` benchmark CLI, follow `arena/README.md` ([benchmark guide](https://github.com/HKUDS/AgentPilot/blob/main/arena/README.md)) and its existing launch/resume commands. Its benchmark configuration does not belong in the core parser.
