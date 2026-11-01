# Configuration Reference

For installation and the reading order, start at [Quick Start](../../README.md#quick-start). This page describes the core TOML fields. Unknown framework keys are rejected; relative paths resolve from the TOML file's directory. `environment.settings` is validated by its adapter.

Templates: [Textual Policy Learning](memory.template.toml), [UCB-based Policy Learning](ucb.template.toml), and a runnable [synthetic example](synthetic.example.toml). The Textual and UCB templates require an adapter and replacement of every `REPLACE_ME` value.

## Policy

Both methods learn the policy for improving agents while keeping language-model weights fixed. UCB-based Policy Learning updates outcome statistics and uses KL-UCB to balance exploration and exploitation. Textual Policy Learning updates reusable lessons stored in Policy Memory.

| Table / field | Purpose |
| --- | --- |
| `policy.method` | Select `"memory"` for Textual Policy Learning or `"ucb"` for UCB-based Policy Learning. |
| `policy.direction_slate_size` | Number of directions in a proposed slate. |
| `policy.max_planning_attempts` | Maximum whole-slate replacements per logical round. |
| `policy.proposer` | Model and investigation limits for proposing changes. |
| `policy.memory.ranker` | Model that ranks directions using Policy Memory; Textual selects the first ranked item. |
| `policy.memory.summarizer` | Model that distills and revises Policy Memory from completed execution and evaluation feedback. |
| `policy.memory.max_chars` | Maximum Policy Memory size. |
| `policy.semantic` | UCB embedding model, calibration identity, and matching thresholds. |
| `policy.cooldown`, `policy.effectiveness`, `policy.neighborhood` | UCB retry, improvement, and exploration rules. |

In the core batch runner, Textual uses one frozen Policy Memory snapshot throughout a batch. After the batch's actions settle, `MemorySummarizer` combines the previous lessons with proposals, code changes, and evaluation outcomes into a validated summary; `SingleFileMemoryCarrier` saves it to the run's `memory.md`. The updated lessons guide the next batch's parent, direction-allocation, and proposal-selection decisions.

The asynchronous Arena research workflow reuses `MemorySummarizer` as build results and their B comparisons settle. It saves revised lessons in the `research-policy/memory` checkpoint for subsequent decisions, without waiting for a whole search batch to finish.

Each completion profile explicitly sets `provider`, `model`, `max_output_tokens`, `temperature`, and `disable_reasoning`; profiles do not inherit one another's values. Provider names must match the adapter's `ProviderRegistry`. For UCB, use the policy tables in the [UCB template](ucb.template.toml); embedding dimensions, identities, and thresholds must match an accepted calibration bundle. Keep your existing environment and worker settings when replacing the policy tables, and finish this configuration before collecting baseline evidence.

For Arena research, edit each policy profile's `model` field separately. The task solver uses `environment.settings.action_runtime.program.model_name`, in the form `openrouter/vendor/model-id`. The coding worker uses its [own model configuration](../integrations/arena/docs/workers.md).

For proposer investigation, `max_proposer_turns` bounds turns; `max_trace_reads` and `max_repository_reads` bound reads; optional `max_read_calls` adds a shared read limit. `max_read_bytes` bounds returned evidence and `max_context_bytes` bounds serialized request history, not the model's token window. See the [research reference](../integrations/arena/docs/research.md) for tool behavior.

## Runtime

| Field under `runtime` | Purpose |
| --- | --- |
| `database_path` | SQLite database for durable run records. |
| `runs_root` | Candidate workspaces and generated artifacts. |
| `worker_slots` | Maximum concurrent actions. |
| `sqlite_timeout_seconds` | SQLite lock timeout. |
| `lease_ttl_seconds` | How long an action ownership claim remains valid. |
| `lease_renew_interval_seconds` | Claim renewal interval; must be shorter than the TTL. |
| `require_provider_reported_cost` | Require authoritative provider cost data; missing data cannot be treated as zero. |

Create the configured output directories before running, and keep them under an ignored directory such as `runs/`.

## Environment Adapter

| Field under `environment` | Purpose |
| --- | --- |
| `factory` | Import path, for example `my_adapter:create_dependencies`. Called with `AppConfig`; returns `AssemblyDependencies`. |
| `identity` | Stable name for the task environment. |
| `settings` | Adapter-specific program, task, worker, and evaluation configuration. |

`AssemblyDependencies` supplies a provider registry and a per-batch environment factory, plus optional UCB calibration. The environment connects durable search inputs, an action runner, a frozen evaluation plan, and an effectiveness rule. See the [core environment interface reference](../docs/environment-adapters.md) for implementation contracts and [worker configuration](../integrations/arena/docs/workers.md) for Arena research workers.

For benchmark-specific task splits and evaluation options, run `agentpilot-arena-aime-build --help` or `agentpilot-arena-tb2-build --help` through `uv run --project agentpilot/integrations/arena --no-sync` from the repository root. Both builders accept `--worker-config` and `--model`; role-specific model flags override `--model`.

## Campaigns and Resume

The optional `campaign` table is used by `agentpilot campaign`:

| Field | Purpose |
| --- | --- |
| `total_processes`, `actions_per_process` | Number of search processes and actions per process. |
| `max_planning_batches_per_process` | Bound on replacement planning batches. |
| `max_stalled_resumes` | Stop after this many consecutive resumes without durable progress. |
| `poll_interval_seconds` | Supervisor polling interval. |
| `reconciliation_deadline_seconds` | Maximum wait for provider accounting. |

Resume requires the original configuration and batch ID. AgentPilot rejects configuration or adapter changes that alter an active batch. In the batch runner, exhausted Policy Memory summarization attempts are a terminal policy-update failure rather than a resumable interruption.

## Credentials

Keep credentials in the environment or a local `.env` based on the repository [`.env.example`](../../.env.example); store only environment-variable names in adapter settings. `--env-file` loads the file for execution, with existing host variables taking precedence. `validate-config` checks framework TOML structure without loading credentials, importing the adapter, or contacting providers.
