# Set Up AgentPilot with Arena

Complete the [repository installation](../../../README.md#quick-start-harness), then follow these four steps from the repository root. This workflow requires Linux, Docker and bubblewrap (`bwrap`); it uses a coding worker to improve a Harbor agent on AIME or Terminal-Bench 2.1.

<a id="worker"></a>

## ① Connect your worker

```bash
mkdir -p runs/my-agent
```

Save your choice from [worker configuration](docs/workers.md) to `runs/my-agent/worker.toml`. The worker edits the program's code; it can use your harness's existing model/login or a provider such as OpenRouter.

<a id="program"></a>

## ② Prepare your program and tasks

If you do not have an initial program or evaluation yet, use the optional [preparation worker](docs/preparation.md). It finds or adapts the missing inputs and returns a structured handoff; other tasks use the custom environment route described there.

This workflow improves the Harbor/Terminus 2 program. Keep its editable checkout separate from the trusted evaluator:

```bash
git clone https://github.com/harbor-framework/harbor.git runs/my-agent/harbor
git -C runs/my-agent/harbor checkout --detach 7db020ba5a5ceee918351dd8fc374d4d60bad442
uv sync --project runs/my-agent/harbor --package harbor --no-dev --locked --python 3.12
git clone --no-hardlinks runs/my-agent/harbor runs/my-agent/program
```

For an existing Harbor program, skip the last command and use its path in `--program` below. Commit its changes before setup. Reuse an existing evaluator checkout by supplying its path in `--harbor`.

| Tasks | Preparation |
| --- | --- |
| AIME 2024+2025 | The 60 tasks and [task split](src/agentpilot_arena/aime/data/split.example.json) are included; step ③ builds the task image. |
| Terminal-Bench 2.1 | Complete [task and image preparation](docs/tb21.md), then continue to ③. |

<a id="policy"></a>

## ③ Choose your policy and model

Start with Textual Policy Learning (`policy.method = "memory"`), which learns reusable lessons in Policy Memory from evaluation feedback while keeping language-model weights fixed. Put `OPENROUTER_API_KEY` in the repository-root `.env`, then replace `vendor/model-id` with an OpenRouter model that supports tool use and structured responses. This model serves the proposer, direction ranker, memory summarizer, and task solver; your worker keeps the configuration from ①. Run the command for your benchmark.

The research command automatically reuses your worker's model/login for proposer, ranker, or summarizer roles whose API credential is absent. To use the worker for all three even with a shared API key available, add `--proposer-access worker --policy-access worker` to the research command in step ④. The task solver still uses its API configuration. See [worker login reuse](docs/workers.md#reuse-the-worker-for-proposer-and-policy-calls).

**AIME:**

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-setup \
  --benchmark aime \
  --output runs/my-agent \
  --program runs/my-agent/program \
  --harbor runs/my-agent/harbor \
  --worker-config runs/my-agent/worker.toml \
  --model vendor/model-id \
  --cell-max-cost-usd 2
```

**Terminal-Bench 2.1:**

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-setup \
  --benchmark tb21 \
  --output runs/my-agent \
  --program runs/my-agent/program \
  --harbor runs/my-agent/harbor \
  --worker-config runs/my-agent/worker.toml \
  --model vendor/model-id \
  --cell-max-cost-usd 2 \
  --tasks runs/my-agent/tb21/tasks \
  --verifier-authority runs/my-agent/tb21/cache/build/offline-verifier-authority.json \
  --docker-inner-address-pool base=10.128.0.0/12,size=24
```

This generates `runs/my-agent/runtime.toml` without model calls. For Terminal-Bench, choose an address pool unused by your Docker network. `--cell-max-cost-usd` sets the benchmark task's cost threshold; an in-flight call can exceed it. The coding worker uses its own configuration from ①.

To use different models for individual roles, edit the generated TOML before step ④; the [configuration reference](../../configs/README.md#policy) lists the fields. UCB-based Policy Learning (`policy.method = "ucb"`) updates outcome statistics instead of textual lessons, requires a calibrated policy configuration, and is described there.

<a id="run"></a>

## ④ Start improving

Collect the starting program's visible-task results:

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-baseline \
  --config runs/my-agent/runtime.toml \
  --output runs/my-agent/baseline \
  --env-file .env --execute \
  --budget-usd 50 --cell-allowance-usd 0.25 \
  --task-pool runs/my-agent/task-pool
```

Then start improvement:

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-arena-research \
  --config runs/my-agent/runtime.toml \
  --evidence runs/my-agent/baseline/packet \
  --output runs/my-agent/search \
  --env-file .env --execute \
  --budget-usd 250 --call-allowance-usd 0.50 --cell-allowance-usd 0.25 \
  --task-pool runs/my-agent/task-pool
```

These commands make model calls, with separate example budgets of USD 50 for initial evaluation and USD 250 for research. Set these budgets and per-call reservation estimates for your workload; they are not price estimates or hard limits on in-flight calls. Both commands default to one benchmark task at a time.

<a id="results"></a>

View progress and results:

```bash
uv run --project agentpilot/integrations/arena --no-sync agentpilot-webui --research-root runs/my-agent/search --serve
```

Open the printed URL. Results are also saved as `session-result.json` and `accepted-artifacts.json` in `runs/my-agent/search`. After an interruption, repeat the research command with `--resume`, keeping its configuration and output unchanged. [Advanced options](docs/research.md).

Developer reference: [core environment interfaces](../../docs/environment-adapters.md).
