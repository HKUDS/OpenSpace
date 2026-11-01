# Run SWE-bench Verified

Complete [Arena installation](../../README.md#quick-start-arena), then run these commands from `arena/`. Supply the official 500-task Harbor-format dataset and replace `/path/to/...` below with local paths. Docker, bubblewrap and the Codex CLI must be available.

## ① Prepare Harbor and the dataset

Install the SWE extra and prepare separate trusted Harbor and candidate checkouts. For existing checkouts, verify the pinned revision instead of cloning again:

```bash
uv sync --locked --extra swe-benchmark

git clone https://github.com/harbor-framework/harbor.git harbor
git -C harbor checkout --detach 7db020ba5a5ceee918351dd8fc374d4d60bad442
uv sync --project harbor --locked --python 3.12

git clone https://github.com/harbor-framework/harbor.git candidate-template
git -C candidate-template checkout --detach 7db020ba5a5ceee918351dd8fc374d4d60bad442
```

The trusted Harbor revision must have tree `03bea3ef9612a7381a2fa2f3955eaeb22324e9ff`. Create the derived snapshot:

```bash
uv run --no-sync rsi-arena-swe-operator prepare-snapshot \
  --source-root /path/to/swe-bench-verified \
  --snapshot-root /path/to/swe-arena-derived
```

The snapshot freezes all 500 tasks and removes reference patches and maintainer hints from verifier inputs. Images are prepared separately below.

## ② Prepare the selected task images

The example below evaluates two development tasks. Prepare both images before launching; repeat this step for any additional tasks you select, including later held-out tasks:

```bash
for task_id in django__django-11099 psf__requests-1142; do
  uv run --no-sync rsi-arena-swe-operator prepare-canary \
    --source-root /path/to/swe-bench-verified \
    --canary-root "/path/to/swe-arena-canary/$task_id" \
    --task-id "$task_id" \
    --harbor-root harbor \
    --candidate-root candidate-template \
    --pull-image \
    --build-images \
    --receipt "/path/to/swe-arena-canary/$task_id.json"
done
```

This downloads images and checks the offline agent/verifier runtime without calling a model. Omit `--pull-image` only when the required images are already present. Keep `builtin:terminus-2` as the inner-agent selection used by the template.

## ③ Configure the experiment

Render the development split and held-out boundary:

```bash
uv run --no-sync rsi-arena-swe-operator render \
  --template examples/swe-bench-verified.toml.template \
  --output runs/swe-canary/experiment.toml \
  --snapshot-root /path/to/swe-arena-derived \
  --harbor-root harbor \
  --campaign-id swe-bench-verified-canary \
  --cell-id codex-swe-canary \
  --method codex \
  --held-out-identity swe-bench-verified-held-out \
  --development-task-id django__django-11099 \
  --development-task-id psf__requests-1142 \
  --a-task-id django__django-11099 \
  --sample-size 1 \
  --sampling-seed 20260903
```

Edit `runs/swe-canary/experiment.toml` before validating it:

- Replace the outer model, inner model and immutable inner-model revision placeholders.
- Set the outer model's actual input/output prices and review `budget_usd`.
- Put `OPENROUTER_API_KEY` in the repository-root `.env`.

```bash
uv run --no-sync --env-file ../.env rsi-arena-swe-operator validate \
  --experiment runs/swe-canary/experiment.toml \
  --snapshot-root /path/to/swe-arena-derived
```

Keep the task-specific timeout requirements: the Harbor wall limit must cover image builds, agent execution, verification and cleanup. The search budget can be exceeded by an already admitted task.

## ④ Run

```bash
uv run --no-sync --env-file ../.env rsi-arena baseline launch \
  --baseline codex \
  --experiment-toml runs/swe-canary/experiment.toml \
  --run-root runs/swe-canary/run \
  --workspace-root candidate-template
```

To continue an interrupted run:

```bash
uv run --no-sync --env-file ../.env rsi-arena baseline resume \
  --baseline codex \
  --run-root runs/swe-canary/run
```

For the selected final candidate, use [held-out evaluation](../README.md#run-held-out-evaluation) with this run's directory and candidate ID.
