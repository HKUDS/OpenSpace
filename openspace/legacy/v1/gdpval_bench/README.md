# GDPVal Benchmark

Benchmark for evaluating OpenSpace on [GDPVal](https://huggingface.co/datasets/openai/gdpval) (220 occupational tasks across 44 occupations and 9 sectors). Measures token savings from skill accumulation by running each task twice:

- **Phase 1** — Cold start. Skills accumulate as tasks run sequentially.
- **Phase 2** — Warm start. Re-run all tasks with the full Phase 1 skill library.

Evaluation uses [ClawWork](https://github.com/HKUDS/ClawWork)'s LLM evaluator (same rubrics, same 0.6 payment cliff).

## Project Layout

```
parent/
├── OpenSpace/                   ← this repo
│   └── gdpval_bench/            ← this directory
└── ClawWork/                    ← required
    ├── eval/meta_prompts/       ← evaluation rubrics
    └── livebench/data/agent_data/  ← ClawWork agent results (for leaderboard)
```

## Setup

```bash
pip install -e . && pip install datasets
git clone https://github.com/HKUDS/ClawWork.git ../ClawWork
pip install -r gdpval_bench/requirements-eval.txt
export OPENROUTER_API_KEY="sk-or-..."
export EVALUATION_API_KEY="sk-..."
```

## Run

```bash
python -u -m gdpval_bench.run_benchmark \
  --task-list gdpval_bench/tasks_50.json \
  --model openrouter/qwen/qwen3.5-plus-02-15 \
  --use-clawwork-productivity \
  --clawwork-root ../ClawWork \
  --resume
```

Key flags: `--phase1-only`, `--phase2-only`, `--no-eval`, `--concurrency N`, `--max-tasks N`, `--prefetch-only`, `--dry-run`.

## Included Data

```
skills/                        # evolved skills
.openspace/openspace.db        # skill & tool quality DB (auto-generated during evolution)
```

`skills/` contains the full skill library produced by evolution — each subdirectory holds a `SKILL.md`. `.openspace/openspace.db` tracks skill lineage, tool quality records, and execution analyses accumulated across benchmark runs.

## Output

```
results/<run_name>/
├── phase1_results.jsonl      # per-task Phase 1
├── phase2_results.jsonl      # per-task Phase 2
├── comparison.jsonl          # Phase 1 vs Phase 2 deltas
├── summary.json              # aggregate statistics
├── skills_snapshot.json      # skills after Phase 1
├── config.json               # run config
├── workspace/                # agent working directories
└── recordings/               # execution trajectories
```

## Analyze

```bash
python -m gdpval_bench.calc_subset_performance
```

Produces leaderboard (OpenSpace vs ClawWork agents), head-to-head comparison, and token savings breakdown.

## Task List

- **[`tasks_50.json`](tasks_50.json)** — 50 task IDs (deterministic subset of GDPVal-220).
- **[`tasks_50_full.jsonl`](tasks_50_full.jsonl)** — Full task data downloaded from [HuggingFace](https://huggingface.co/datasets/openai/gdpval). One JSON object per line with all original fields (`task_id`, `sector`, `occupation`, `prompt`, `reference_files`, `reference_file_urls`, `rubric_json`, etc.). Covers all 9 sectors and 44 occupations.
