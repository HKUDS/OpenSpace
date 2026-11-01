# Advanced Research Reference

For installation, configuration, and a normal run, follow the [AgentPilot + Arena guide](../README.md#run). This reference is optional.

## Model Access

`--proposer-access auto` uses the proposer's configured API key when present and otherwise reuses the configured worker CLI's model/login. `--policy-access auto` applies the same rule independently to Memory ranking and summarization. Both default to `auto`; choose `worker` to require CLI reuse or `api` to require an API key. API failures never silently switch routes. All supported worker backends use the same [completion contract](workers.md#reuse-the-worker-for-proposer-and-policy-calls).

Access choices and effective role models are saved in `completion-routes`. Resuming preserves them; if newly available credentials change automatic selection, explicitly select the original access mode or start a new output directory. CLI calls retain their per-call budget allowances and record external billing, without inventing a per-call price. Task evaluation and UCB embeddings still need their configured providers.

## Research Tools

| Tool | Behavior |
| --- | --- |
| `experiment(instruction=...)` | Run a worker experiment; retain its report, scripts, receipts, and source changes. A/B evaluation is not automatic. |
| `read_experiment(ticket="X1")` | Read a result; omit the ticket to browse history, or use `track="T1"` for a research line's current judgment and evidence. |
| `list_cases(view="tasks")` | Read visible-A task requirements without answers, outcomes, or trace bodies. |
| `build(instruction=...)` | Implement and check a candidate, then automatically compare it with the working version on B when admission is configured. |
| `build(from_experiment="X1", instruction=...)` | Adopt an experiment's retained source changes before finishing the candidate. |
| `evaluate(artifact="C1")` | Compare an existing candidate on B without rebuilding it. |
| `note`, `drop`, `close` | Save a judgment, stop new work on a track, or end the conversation while retaining running jobs and results. |

Experiments can call configured proposer/solver models through host-managed helpers and explicitly request visible-A measurements, including task subsets. Citing an experiment in build instructions passes its findings; `from_experiment` also adopts its edits. Temporary edits become candidates only through a build. Local findings inform research and direction ranking in Textual Policy Learning; valid B comparisons provide effectiveness observations. Experiment tools expose limited B summaries and no raw B or held-out C material.

## Judgments and Asynchronous Work

The proposer reads task requirements and source, then records a provisional explanation or capability idea, an alternative, and evidence that could refute it. A single task, a local repair, or a broader capability change is a valid scope. There is no mandatory hypothesis form or gate on trace reads. Contradictory findings should revise or end a research line.

`note(text=..., track="new")` creates a T identifier. Later notes record changed judgments, uncertainty, and supporting X tickets. Unassociated notes remain session notes. Requirements come from task briefs or recorded initial user input, with truncation marked; later agent explanations are not treated as requirements.

Independent reads and jobs can continue while an experiment runs. When the next decision needs its result, a nonempty assistant note without tool calls yields execution. The controller saves that wait and wakes when work completes; waiting does not spend model calls on polling.

Saved judgments have three consumers:

- Later proposer requests receive relevant judgments and result previews, including after compaction.
- Workers receive related judgments and cited reports, scripts, and outputs. Submitted worker inputs are frozen across restarts.
- The inspector shows worker observations separately from proposer judgments; it does not generate another narrative.

A completed experiment does not prove its hypothesis. Commits, tracks, tickets, and session checkpoints persist independently.

## Observation Modes

Add these flags to the [research command](../README.md#run). Both observation modes require `--execute`, make real model calls, and need their own `--output` directory. An existing output cannot switch between observation and normal research modes.

| Flag | Behavior |
| --- | --- |
| `--proposer-only` | Record source/trace reads and notes; stop before experiment, build, or evaluation dispatch. Historical evidence need not match the current measurement apparatus. |
| `--experiments-only` | Run experiments and return results to the proposer; stop before builds or B admission. Visible-A task measurements are allowed. |
| `--turns` | In experiments-only mode, bound investigation calls before the first completed experiment. |
| `--feedback-turns` | Reserve post-result calls in experiments-only mode; default 16. |
| `--max-experiments` | Bound submitted experiments in experiments-only mode; default 2. |

Experiments-only checks the runtime/tasks and records historical and current measurement identities without relabeling results or relaxing B validation. Limits persist across resume; launched jobs settle after an observation boundary. Proposer-only saves `proposer-input.json`, `proposer-observations.json`, `proposer-conversation.json`, and `session-result.json`; requested work has not executed.

## Recovery

- `--resume` continues the saved conversation with the same configuration, evidence, model, and output. `--session NAME` starts a new conversation; `--rounds` plans a persisted sequence.
- Completed measurements and durable receipts are reused. An unknown in-flight outcome needs reconciliation; missing receipts neither mean zero cost nor authorize another request.
- Reservations are estimates, and in-flight calls settle at actual cost. Changing a frozen runtime or measurement apparatus requires a separate run.
- Legacy measurement hashes are not rewritten; use their original environment or collect a new baseline.

<details>
<summary>Optional: fixed-profile multi-benchmark studies</summary>

`run_formal_research.py` uses fixed model profiles and frozen source for policy studies; it is not the normal installation path. Inspect generated TOMLs and `experiment.json` before dispatch.

From the repository root:

```bash
uv run --project agentpilot/integrations/arena --no-sync python agentpilot/experiments/run_formal_research.py prepare --output runs/formal-research --aime-config /path/to/aime-runtime.toml --tb21-config /path/to/tb21-runtime.toml
uv run --project agentpilot/integrations/arena --no-sync python agentpilot/experiments/run_formal_research.py preflight --output runs/formal-research --benchmarks aime tb21 --methods memory --rounds 5 --budget-usd 200
```

Replace `preflight` with `run` and add `--env-file .env` to execute; use `resume` with the original settings to continue. Here USD 200 is allocated per benchmark, including its shared baseline. `--baseline-repeats` sets A repetitions, and a shared task pool limits concurrency. `reconcile` retrieves existing receipts without new inference.

UCB requires `--ucb-calibration`. Optional `--provider-selection` freezes routes and output limits; `--host-tmpdir` selects a short directory for local sockets.

</details>

Use the [progress viewer](../README.md#results) to inspect jobs, judgments, lineage, code changes, policy and accounting. For formal studies, set `--research-root runs/formal-research`.

For existing single-action campaigns, see the [legacy campaign reference](legacy-campaigns.md).
