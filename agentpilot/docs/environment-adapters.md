# Core Environment Interface Reference

This reference is for developers implementing AgentPilot's environment bindings. Adding a benchmark through these interfaces requires implementing the task source, action runner, evaluation plan and result integration. The existing runnable workflow and coding-worker setup are documented in the [AIME/Terminal-Bench setup guide](../integrations/arena/README.md).

If the initial program or evaluation is missing, the [preparation workflow](../integrations/arena/docs/preparation.md) can find reusable inputs and check an existing adapter. When no compatible adapter exists, implementing these bindings is a separate development task; preparation alone cannot launch the formal loop.

After cloning the repository through [Quick Start](../../README.md#quick-start), install the core from the repository root:

```bash
uv sync --project agentpilot --locked --python 3.12 --extra providers
```

For an offline smoke run, use the synthetic example (requires Git). Its outcomes and costs are simulated:

```bash
mkdir -p agentpilot/runs
uv run --project agentpilot --no-sync agentpilot run --config agentpilot/configs/synthetic.example.toml --run-id example --batch-id example-1 --rounds 2
```

The `native` / `provider` worker options are currently wired into the Arena research workflow. A custom core adapter supplies its own action runner; setting those worker options alone does not create one.

## Adapter Interface

Set `environment.factory` to an importable callable such as `my_adapter:create_dependencies`. AgentPilot calls it with `AppConfig`; it returns `AssemblyDependencies` containing a `ProviderRegistry`, an environment factory, and an optional UCB semantic calibration bundle.

The environment factory receives `BatchAssemblyContext` for each batch and returns `Environment`:

| Required field | Responsibility |
| --- | --- |
| `search_inputs` | A `DurableSearchInputSource` exposing the program and recorded results. Seed the initial program and baseline through `CanonicalSearchReadModelWriter`. |
| `action_runner` | An `ActionRunnerBinding` that runs the worker and evaluation under the active execution lease. |
| `evaluation_plan_freezer` | Freeze the tasks and paired measurements before actions execute. |
| `effectiveness_rule` | Decide whether the measured change is effective. |
| `trace_store` | A run-bound `TraceStore` with persisted Visible-A trace captures and manifests for each selectable program. |
| `definition_reader` | Read definitions from the selected parent's immutable source snapshot. |

UCB also needs compatible embedding providers, calibration, neighborhood predicates, and an applicability builder. The [synthetic adapter](../src/agentpilot/examples/synthetic_adapter.py) shows the complete interface; it uses deterministic stand-ins for external workers and models. The exact types are in [bindings.py](../src/agentpilot/app/bindings.py).

## Run an Implemented Adapter

After implementing the adapter and making its package importable in the AgentPilot environment, copy the [Textual Policy Learning template](../configs/memory.template.toml) to `agentpilot/configs/my-agent.toml`. Replace every `REPLACE_ME` value, set your factory and model profiles, and supply any required credentials in `.env`. The [configuration reference](../configs/README.md) explains the fields.

From the repository root, using the template's default output paths:

```bash
mkdir -p agentpilot/runs
uv run --project agentpilot --no-sync agentpilot run --config agentpilot/configs/my-agent.toml --env-file .env --run-id my-run --batch-id batch-1 --rounds 2
```

Use fresh run and batch IDs for a new run. To continue an interrupted batch, keep its configuration and IDs; see [campaigns and resume](../configs/README.md#campaigns-and-resume).
