# Prepare Inputs and Continue AgentPilot

Use this step when the initial program or evaluation is missing. Reuse compatible inputs and implemented adapters; a dataset or test command alone is not a runnable AgentPilot environment. Generic adapter development is a separate task. Working configurations can continue through the normal [setup and run guide](../README.md).

## Prepare with the current agent

In the installed [integration environment](../../../../README.md#quick-start-harness), generate a preparation prompt:

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-prepare \
  --goal "Prepare the starting program and comparable evaluation for the actual task" \
  --output runs/my-agent/preparation
```

Add `--program /path/to/program` or `--eval /path/to/evaluation` for existing inputs. Read the generated `prompt.md` and complete it with the current coding agent. Keep supplied inputs unchanged; put adaptations in a separate workspace. Reuse a suitable clean program revision and preserve the user's edits and commit restrictions. If the required revision or adapter is unavailable, record the missing prerequisite instead of silently creating a commit or changing the task.

To delegate preparation to an already configured worker, add `--worker-config /path/to/worker.toml --execute`, and optionally the configured `--env-file`. This makes one worker call; it does not start improvement. Worker charges remain external, and its reservation is neither a hard cap nor a verified bill. A separate worker is unnecessary when the current agent is preparing the inputs.

Use a fresh `--output`; existing directories are not overwritten and failed worker calls are not retried automatically. Preserve useful files and complete the same delivery before checking it:

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-prepare \
  --check runs/my-agent/preparation/prepared.json
```

## Delivery and readiness

| File | Purpose |
| --- | --- |
| `prompt.md`, `request.json` | Preparation instructions and original request. |
| `prepared.json` | Program revision, evaluation route, artifact paths and provenance, or a blocked result. |
| `PREPARATION.md` | Installation, actual smoke commands/logs, metric and comparison protocol, missing settings and runner invocation. |
| `worker-result.json` | Diagnostics and external accounting for delegated preparation. |

The `agentpilot-preparation/v1` handoff keeps evaluation resources outside the editable program. Paths resolve from the manifest directory. `--check` validates files, Git identity and configuration structure; it does not execute or certify the adapter.

Before improvement, verify that the implemented adapter uses the intended program and evaluator, and that their current identities match the actual smoke results. Evaluation must measure the user's objective with meaningful cases, comparable inputs and correctness constraints; noisy metrics need appropriate repeats. Ordinary correctness tests do not demonstrate a speedup. Preserve the adapter's development/comparison/held-out protocol.

Arena baseline `complete` can include invalid captures. Inspect measurements and diagnostics: a valid wrong answer or zero score is usable evidence; unresolved apparatus failures can make the intended comparison unmeasurable. Unknown charges remain unknown, never zero. Ensure the existing provider, worker, environment and authorized budget are available. The research runner can [reuse worker login](workers.md#reuse-the-worker-for-proposer-and-policy-calls) for proposer and Memory policy calls; task evaluation and UCB embeddings still require their configured providers.

## Continue into the real runner

When inputs and prerequisites are ready within the user's request, the current agent must invoke the configured AgentPilot runner, rather than only print a command or continue its own optimization loop. AgentPilot owns direction selection, worker dispatch, evaluation and policy feedback. If readiness cannot be established, report preparation incomplete and the specific missing prerequisite.

For Harbor/Terminus 2 on AIME or Terminal-Bench 2.1, use `evaluation.kind = "arena_setup"`. Import the handoff with `agentpilot-arena-setup --prepared /path/prepared.json` and the existing worker/model/environment choices from the [setup guide](../README.md#policy). Then execute its [baseline and research commands](../README.md#run). Reuse matching existing runtime/evidence instead of repeating work. Use the user's existing or established budgets; guide amounts are examples, not defaults. Resume baseline collection with the same arguments and output, and research with the same configuration/evidence/output plus `--resume`.

For another task with an **implemented** adapter, use `evaluation.kind = "core_config"`. The [core environment binding](../../../docs/environment-adapters.md) must already provide program seeding, real worker execution, frozen paired evaluation and feedback. Install it in the selected environment and run its documented smoke check before the core commands:

```text
agentpilot validate-config --config /path/runtime.toml
agentpilot run --config /path/runtime.toml --run-id RUN --batch-id BATCH --rounds N
agentpilot resume --config /path/runtime.toml --batch-id BATCH
```

Use the configured run length and credentials. Preserve the original batch and configuration for recovery; fresh IDs start a new run. Custom core adapters are not automatically registered with the built-in Arena research CLI. If no compatible binding exists, deliver a blocked handoff and explain the adapter work needed.
