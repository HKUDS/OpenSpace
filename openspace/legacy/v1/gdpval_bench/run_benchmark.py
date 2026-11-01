#!/usr/bin/env python3
"""
GDPVal Benchmark — Two-phase experiment for OpenSpace skill-driven token savings.

Design:
  Phase 1 (Cold → Warm):  Run 220 tasks, skills accumulate across tasks.
  Phase 2 (Full Warm):    Run the same 220 tasks again with all Phase 1 skills.

For each task, records:
  - Token usage (prompt, completion, total, cost)
  - Execution metrics (iterations, tool calls, time, status)
  - Skills state (count before/after, which skills were used)

Produces:
  results/<run_name>/
    ├── phase1_results.jsonl     # Per-task results from Phase 1
    ├── phase2_results.jsonl     # Per-task results from Phase 2
    ├── skills_snapshot.json     # All skills after Phase 1
    ├── comparison.jsonl         # Per-task Phase 1 vs Phase 2 comparison
    ├── summary.json             # Aggregate statistics
    └── recordings/              # OpenSpace recordings per task
        ├── phase1/
        └── phase2/

Usage:
    # Full run (220 tasks × 2 phases)
    python -m gdpval_bench.run_benchmark

    # Quick test (5 tasks)
    python -m gdpval_bench.run_benchmark --max-tasks 5

    # Concurrent (3 workers per phase)
    python -m gdpval_bench.run_benchmark --concurrency 3

    # Resume from where you left off
    python -m gdpval_bench.run_benchmark --resume

    # Phase 2 only (requires Phase 1 completed)
    python -m gdpval_bench.run_benchmark --phase2-only

    # Custom config
    python -m gdpval_bench.run_benchmark --config gdpval_bench/config.json

Fair comparison with ClawWork:
    Set "use_clawwork_productivity": true in config (or pass --use-clawwork-productivity when added).
    This adds the same productivity tools as ClawWork (search_web, read_webpage, create_file, read_file,
    execute_code_sandbox, create_video) so benchmark results are comparable. Requires livebench (ClawWork)
    to be installed in the same environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Ensure OpenSpace is importable ──
_OPENSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_OPENSPACE_ROOT))

# ── Load .env (same logic as openspace/llm/client.py) ──
try:
    from dotenv import load_dotenv
    _pkg_env = _OPENSPACE_ROOT / "openspace" / ".env"
    if _pkg_env.is_file():
        load_dotenv(_pkg_env)
    load_dotenv()  # also try CWD/.env
except ImportError:
    pass  # dotenv not installed; rely on shell env vars

from gdpval_bench.token_tracker import TokenTracker, TokenStats
from gdpval_bench.task_loader import load_tasks, prepare_task_workspace

# ── Default paths ──
_DEFAULT_CONFIG = Path(__file__).parent / "config.json"
_DEFAULT_RESULTS = Path(__file__).parent / "results"
_OPENSPACE_DB_DIR = _OPENSPACE_ROOT / ".openspace"

# ── Evaluation constants (aligned with ClawWork) ──
# Artifact extensions that ClawWork considers for evaluation
_ARTIFACT_EXTENSIONS = {
    '.pdf', '.docx', '.xlsx', '.pptx',      # documents
    '.txt', '.csv', '.json', '.md',          # text
    '.py', '.js', '.html', '.css',           # code
    '.png', '.jpg', '.jpeg', '.gif', '.webp', # images
}
# Minimum evaluation score to receive payment (ClawWork cliff)
_MIN_EVALUATION_THRESHOLD = 0.6


# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load experiment config with sensible defaults."""
    defaults = {
        "clawwork_root": str(_OPENSPACE_ROOT.parent / "ClawWork"),
        "gdpval_path": None,
        "model": "openrouter/qwen/qwen3.5-plus-02-15",
        "max_iterations": 20,
        "backend_scope": ["shell"],
        "use_clawwork_productivity": False,
        "run_name": datetime.now().strftime("gdpval_%Y%m%d_%H%M%S"),
        "max_tasks": None,
        "per_occupation": None,
        "sectors": None,
        "occupations": None,
        "task_ids": None,
        "record_call_details": True,
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r") as f:
            overrides = json.load(f)
        defaults.update(overrides)

    return defaults


# ═══════════════════════════════════════════════════════════════════
# Result I/O
# ═══════════════════════════════════════════════════════════════════

def _results_dir(cfg: Dict) -> Path:
    name = cfg.get("run_name")
    if not name:
        model = cfg.get("model", "unknown")
        # e.g. "openrouter/qwen/qwen3.5-plus-02-15" → "qwen3.5-plus-02-15"
        short_model = model.rsplit("/", 1)[-1] if "/" in model else model
        name = f"{short_model}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cfg["run_name"] = name
    return _DEFAULT_RESULTS / name


def _append_jsonl(path: Path, record: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _completed_task_ids(results_file: Path) -> set:
    """Return set of task_ids already completed (for resume)."""
    return {r["task_id"] for r in _load_jsonl(results_file) if r.get("status")}


# ═══════════════════════════════════════════════════════════════════
# Skill snapshot
# ═══════════════════════════════════════════════════════════════════

def _snapshot_skills(skill_store) -> List[Dict[str, Any]]:
    """Dump all active skills from the SkillStore."""
    try:
        records = skill_store.load_active()  # Returns Dict[str, SkillRecord]
        return [r.to_dict() for r in records.values()]
    except Exception as e:
        print(f"⚠️  Could not snapshot skills: {e}")
        return []


def _count_skills_by_origin(skills: List[Dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for s in skills:
        origin = s.get("lineage", {}).get("origin", "unknown")
        counts[origin] = counts.get(origin, 0) + 1
    return counts


# ═══════════════════════════════════════════════════════════════════
# Evaluation — aligned with ClawWork's LLMEvaluator
# ═══════════════════════════════════════════════════════════════════

def _discover_artifacts(
    workspace_dir: str,
    reference_filenames: List[str],
) -> List[str]:
    """Discover agent-created artifacts in the workspace.

    Scans ``workspace_dir`` for files matching ``_ARTIFACT_EXTENSIONS`` that
    are NOT the downloaded reference files (to avoid evaluating input files).

    This mirrors how ClawWork's ``list_artifacts`` discovers work products.

    Returns:
        Sorted list of absolute paths to artifact files.
    """
    ws = Path(workspace_dir)
    if not ws.exists():
        return []

    ref_names = set(reference_filenames)
    artifacts: List[str] = []

    for f in ws.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _ARTIFACT_EXTENSIONS:
            continue
        if f.name in ref_names:
            continue
        # Skip zero-byte files
        if f.stat().st_size == 0:
            continue
        artifacts.append(str(f))

    return sorted(artifacts)


def _get_evaluator(cfg: Dict):
    """Lazily create and cache a ClawWork-compatible LLMEvaluator.

    Uses the same initialization logic as ClawWork:
    - Model: gpt-4o (or ``EVALUATION_MODEL`` env override)
    - API key: ``EVALUATION_API_KEY`` > ``OPENAI_API_KEY``
    - API base: ``EVALUATION_API_BASE`` > ``OPENAI_API_BASE``
    - Meta-prompts: ``ClawWork/eval/meta_prompts/``

    The evaluator is created once and reused for all tasks.
    """
    if hasattr(_get_evaluator, "_instance"):
        return _get_evaluator._instance

    clawwork_root = Path(cfg.get("clawwork_root", ""))
    meta_prompts_dir = clawwork_root / "eval" / "meta_prompts"

    if not meta_prompts_dir.exists():
        print(f"  ⚠️  Meta-prompts dir not found: {meta_prompts_dir}")
        print(f"       Evaluation will be skipped.")
        _get_evaluator._instance = None
        return None

    # Import from ClawWork
    clawwork_livebench = clawwork_root / "livebench"
    if str(clawwork_livebench.parent) not in sys.path:
        sys.path.insert(0, str(clawwork_livebench.parent))

    try:
        from livebench.work.llm_evaluator import LLMEvaluator

        evaluator = LLMEvaluator(
            meta_prompts_dir=str(meta_prompts_dir),
            max_payment=50.0,  # default; will be overridden per-task
        )
        _get_evaluator._instance = evaluator
        return evaluator
    except Exception as e:
        print(f"  ⚠️  Could not initialize LLMEvaluator: {e}")
        _get_evaluator._instance = None
        return None


def _evaluate_task(
    task: Dict,
    workspace_dir: str,
    cfg: Dict,
) -> Dict[str, Any]:
    """Evaluate agent artifacts using ClawWork's LLMEvaluator.

    Fully aligned with ClawWork's evaluation pipeline:
      1. Discover artifacts in workspace (same extensions as ClawWork)
      2. Call ``LLMEvaluator.evaluate_artifact`` (same model, system prompt,
         meta-prompt rubric, and evaluation prompt template)
      3. Apply 0.6 payment cliff (same as ``EconomicTracker.add_work_income``)
      4. Return structured result matching ClawWork's output format

    Returns dict with keys:
        evaluation_score, score_10, payment, actual_payment,
        artifact_paths, description, feedback (truncated), has_evaluation
    """
    # ── Discover artifacts ──
    ref_filenames = [
        Path(rf).name for rf in (task.get("reference_files", []) or [])
    ]
    artifact_paths = _discover_artifacts(workspace_dir, ref_filenames)

    if not artifact_paths:
        return {
            "has_evaluation": False,
            "evaluation_score": 0.0,
            "score_10": 0,
            "payment": 0.0,
            "actual_payment": 0.0,
            "artifact_count": 0,
            "artifact_paths": [],
            "description": "",
            "feedback": "No artifacts found in workspace.",
        }

    evaluator = _get_evaluator(cfg)
    if evaluator is None:
        return {
            "has_evaluation": False,
            "evaluation_score": 0.0,
            "score_10": 0,
            "payment": 0.0,
            "actual_payment": 0.0,
            "artifact_count": len(artifact_paths),
            "artifact_paths": [os.path.basename(p) for p in artifact_paths],
            "description": "",
            "feedback": "Evaluator not available (missing meta-prompts or API key).",
        }

    # ── Build description (same format as ClawWork's submit_work) ──
    description = f"Work submission with {len(artifact_paths)} artifact(s)"

    # ── Task-specific max payment ──
    max_payment = task.get("task_value_usd", 0.0) or 50.0

    try:
        evaluation_score, feedback, payment = evaluator.evaluate_artifact(
            task=task,
            artifact_paths=artifact_paths,
            description=description,
            max_payment=max_payment,
        )
    except Exception as e:
        err_msg = str(e)
        print(f"  ⚠️  Evaluation failed: {e}")
        if "langchain" in err_msg.lower():
            print(f"     Fix: pip install langchain_core  (livebench evaluator dependency)")
        return {
            "has_evaluation": False,
            "evaluation_score": 0.0,
            "score_10": 0,
            "payment": 0.0,
            "actual_payment": 0.0,
            "artifact_count": len(artifact_paths),
            "artifact_paths": [os.path.basename(p) for p in artifact_paths],
            "description": description,
            "feedback": f"Evaluation error: {e}",
        }

    # ── Apply 0.6 cliff (same as ClawWork EconomicTracker) ──
    if evaluation_score < _MIN_EVALUATION_THRESHOLD:
        actual_payment = 0.0
    else:
        actual_payment = payment

    # Truncate feedback for JSONL storage (keep first 500 chars)
    feedback_short = feedback[:500] + "..." if len(feedback) > 500 else feedback

    return {
        "has_evaluation": True,
        "evaluation_score": round(evaluation_score, 4),
        "score_10": round(evaluation_score * 10, 1),
        "payment": round(payment, 2),
        "actual_payment": round(actual_payment, 2),
        "max_payment": round(max_payment, 2),
        "artifact_count": len(artifact_paths),
        "artifact_paths": [os.path.basename(p) for p in artifact_paths],
        "description": description,
        "feedback": feedback_short,
        "cliff_applied": evaluation_score < _MIN_EVALUATION_THRESHOLD,
    }


# ═══════════════════════════════════════════════════════════════════
# Helper: create OpenSpaceConfig
# ═══════════════════════════════════════════════════════════════════

def _make_config(cfg: Dict, phase: str, worker_id: int = 0):
    """Create a OpenSpaceConfig for one worker."""
    from openspace.tool_layer import OpenSpaceConfig

    rd = _results_dir(cfg)
    # Each worker gets its own recording dir to avoid collisions
    rec_suffix = f"w{worker_id}" if worker_id > 0 else ""
    rec_dir = str(rd / "recordings" / phase / rec_suffix) if rec_suffix else str(rd / "recordings" / phase)

    return OpenSpaceConfig(
        llm_model=cfg["model"],
        workspace_dir=str(rd / "workspace" / phase),
        recording_log_dir=rec_dir,
        recording_backends=cfg.get("backend_scope", ["shell", "web"]),
        backend_scope=cfg.get("backend_scope"),
        use_clawwork_productivity=cfg.get("use_clawwork_productivity", False),
        grounding_max_iterations=cfg.get("max_iterations", 20),
        enable_recording=True,
        enable_screenshot=False,
        enable_video=False,
        enable_conversation_log=True,
    )


# ═══════════════════════════════════════════════════════════════════
# Helper: execute a single task and build the result record
# ═══════════════════════════════════════════════════════════════════

async def _execute_one_task(
    cs,
    task: Dict,
    phase: str,
    cfg: Dict,
    idx: int,
    total: int,
    token_stats: TokenStats,
    elapsed: float,
) -> Dict[str, Any]:
    """Build a result record (called after cs.execute finishes)."""
    # This is a helper only for record building; see _run_single_task for execution.
    raise NotImplementedError("Use _run_single_task instead")


async def _run_single_task(
    cs,
    task: Dict,
    phase: str,
    cfg: Dict,
    idx: int,
    total: int,
    tracker: TokenTracker,
    results_file: Path,
    results_list: List[Dict],
    *,
    concurrent: bool = False,
) -> Dict[str, Any]:
    """Execute one task on the given OpenSpace instance and persist the result.

    In concurrent mode, uses tracker.begin_task/end_task with ContextVar.
    In serial mode, uses tracker.start/stop.
    """
    rd = _results_dir(cfg)
    tid = task["task_id"]

    print(f"\n{'='*60}")
    print(f"[{phase.upper()}] Task {idx}/{total}: {task['occupation']}")
    print(f"  ID: {tid}")
    print(f"  Prompt: {task['prompt'][:120]}...")
    print(f"{'='*60}")

    # Skill count before execution
    skills_before = 0
    if cs._skill_store:
        try:
            skills_before = len(cs._skill_store.load_active())
        except Exception:
            pass

    # ── Token tracking setup ──
    ctx_token = None
    if concurrent:
        ctx_token = tracker.begin_task(tid)
    else:
        tracker.start()

    t0 = time.monotonic()

    try:
        exec_task_id = f"{tid}_{phase}"
        task_workspace = str(rd / "workspace" / phase / tid)

        # Download reference files & augment prompt with file locations
        augmented_prompt = prepare_task_workspace(task, task_workspace)
        n_refs = len(task.get("reference_files", []) or [])
        if n_refs:
            print(f"  📎 {n_refs} reference file(s) → {task_workspace}")

        result = await cs.execute(
            task=augmented_prompt,
            task_id=exec_task_id,
            workspace_dir=task_workspace,
        )
        exec_status = result.get("status", "unknown")
    except Exception as e:
        result = {"status": "error", "error": str(e)}
        exec_status = "error"
        print(f"  ❌ Error: {e}")

    elapsed = time.monotonic() - t0

    # ── Token tracking teardown ──
    if concurrent:
        stats = tracker.end_task(tid, ctx_token)
    else:
        stats = tracker.stop()

    # Skill count after execution
    skills_after = 0
    evolved_skills = result.get("evolved_skills", [])
    if cs._skill_store:
        try:
            skills_after = len(cs._skill_store.load_active())
        except Exception:
            pass

    # ── Evaluate artifacts (aligned with ClawWork) ──
    eval_result: Dict[str, Any] = {"has_evaluation": False}
    if cfg.get("enable_evaluation", True) and exec_status != "error":
        try:
            eval_result = _evaluate_task(task, task_workspace, cfg)
            if eval_result.get("has_evaluation"):
                score_10 = eval_result["score_10"]
                actual_pay = eval_result["actual_payment"]
                cliff = " (⚠️ below 0.6 cliff)" if eval_result.get("cliff_applied") else ""
                print(f"  📝 Evaluation: {score_10}/10 → ${actual_pay:.2f}{cliff}")
                print(f"     Artifacts: {eval_result['artifact_paths']}")
            else:
                print(f"  📝 Evaluation skipped: {eval_result.get('feedback', 'N/A')}")
        except Exception as e:
            print(f"  ⚠️  Evaluation error: {e}")

    # Build result record
    record = {
        "task_id": tid,
        "phase": phase,
        "occupation": task.get("occupation", ""),
        "sector": task.get("sector", ""),
        "task_value_usd": task.get("task_value_usd", 0.0),
        "status": exec_status,
        "tokens": stats.to_dict(include_details=cfg.get("record_call_details", False)),
        "execution": {
            "iterations": result.get("iterations", 0),
            "tool_calls": len(result.get("tool_executions", [])),
            "time_sec": round(elapsed, 2),
        },
        "skills": {
            "before": skills_before,
            "after": skills_after,
            "new_this_task": skills_after - skills_before,
            "evolved": [
                {
                    "name": s.get("name", ""),
                    "origin": s.get("origin", ""),
                    "path": s.get("path", ""),
                }
                for s in evolved_skills
            ],
            "used": result.get("skills_used", []),
        },
        "evaluation": eval_result,
        "timestamp": datetime.now().isoformat(),
    }

    # Persist immediately (crash-safe)
    _append_jsonl(results_file, record)
    results_list.append(record)

    # Print summary
    _print_task_summary(record)

    return record


# ═══════════════════════════════════════════════════════════════════
# Phase runner — serial (concurrency=1)
# ═══════════════════════════════════════════════════════════════════

async def _run_phase_serial(
    phase: str,
    tasks: List[Dict],
    cfg: Dict,
    tracker: TokenTracker,
    completed_ids: set,
) -> List[Dict[str, Any]]:
    """Run one phase sequentially. A single OpenSpace instance is reused
    so that skills accumulate within the phase."""
    from openspace.tool_layer import OpenSpace

    rd = _results_dir(cfg)
    results_file = rd / f"{phase}_results.jsonl"
    config = _make_config(cfg, phase)

    cs = OpenSpace(config=config)
    await cs.initialize()

    results: List[Dict] = []
    total = len(tasks)
    skipped = 0

    try:
        for idx, task in enumerate(tasks, 1):
            if task["task_id"] in completed_ids:
                skipped += 1
                continue

            await _run_single_task(
                cs, task, phase, cfg, idx, total,
                tracker, results_file, results,
                concurrent=False,
            )
    finally:
        await cs.cleanup()

    if skipped:
        print(f"\n📌 Skipped {skipped} already-completed tasks (resume mode)")

    return results


# ═══════════════════════════════════════════════════════════════════
# Phase runner — concurrent (concurrency>1)
# ═══════════════════════════════════════════════════════════════════

async def _run_phase_concurrent(
    phase: str,
    tasks: List[Dict],
    cfg: Dict,
    tracker: TokenTracker,
    completed_ids: set,
    concurrency: int,
) -> List[Dict[str, Any]]:
    """Run one phase with N concurrent OpenSpace workers.

    Trade-offs vs serial:
      - Skills from concurrent tasks may not be visible to each other (reduced
        accumulation within a batch), but completed tasks' skills are visible
        to later tasks.
      - skills.before / skills.after counts are approximate when workers overlap.
      - Significantly faster wall-clock time.

    Architecture:
      - N OpenSpace instances created up front (worker pool via asyncio.Queue)
      - Each asyncio.Task grabs a worker, executes, returns it to the pool
      - Token tracking uses ContextVar so litellm callbacks route to the
        correct per-task bucket automatically.
    """
    from openspace.tool_layer import OpenSpace

    rd = _results_dir(cfg)
    results_file = rd / f"{phase}_results.jsonl"

    # ── Filter pending tasks ──
    pending = [t for t in tasks if t["task_id"] not in completed_ids]
    if not pending:
        print(f"  All {len(tasks)} tasks already completed (resume mode)")
        return []

    skipped = len(tasks) - len(pending)
    if skipped:
        print(f"📌 Skipped {skipped} already-completed tasks (resume mode)")

    # ── Create worker pool ──
    actual_concurrency = min(concurrency, len(pending))
    print(f"🔧 Creating {actual_concurrency} OpenSpace workers...")

    pool: asyncio.Queue = asyncio.Queue()
    workers: List[OpenSpace] = []

    for i in range(actual_concurrency):
        config = _make_config(cfg, phase, worker_id=i)
        cs = OpenSpace(config=config)
        await cs.initialize()
        workers.append(cs)
        pool.put_nowait(cs)
        print(f"  ✅ Worker {i} ready")

    # ── Install persistent callback for concurrent token tracking ──
    tracker.install()

    results: List[Dict] = []
    total_tasks = len(tasks)
    completed = 0
    errors = 0

    async def process_task(idx: int, task: Dict) -> None:
        nonlocal completed, errors
        cs = await pool.get()  # blocks until a worker is available
        try:
            record = await _run_single_task(
                cs, task, phase, cfg, idx, total_tasks,
                tracker, results_file, results,
                concurrent=True,
            )
            if record.get("status") == "error":
                errors += 1
        except Exception as e:
            errors += 1
            print(f"  ❌ [{task['task_id']}] Unhandled error: {e}")
        finally:
            pool.put_nowait(cs)  # return worker to pool
            completed += 1
            remaining = len(pending) - completed
            if remaining > 0 and completed % 5 == 0:
                print(f"\n  📊 Progress: {completed}/{len(pending)} done, "
                      f"{remaining} remaining, {errors} errors")

    # ── Dispatch all tasks as asyncio Tasks ──
    # The pool.get() naturally limits concurrency to N workers
    async_tasks = []
    for idx, task in enumerate(pending, 1 + skipped):
        async_tasks.append(asyncio.create_task(process_task(idx, task)))

    await asyncio.gather(*async_tasks, return_exceptions=True)

    # ── Cleanup ──
    tracker.uninstall()

    print(f"\n🔧 Cleaning up {len(workers)} workers...")
    for i, cs in enumerate(workers):
        try:
            await cs.cleanup()
        except Exception as e:
            print(f"  ⚠️ Worker {i} cleanup error: {e}")

    print(f"  ✅ All workers cleaned up")
    return results


# ═══════════════════════════════════════════════════════════════════
# Phase runner — unified entry point
# ═══════════════════════════════════════════════════════════════════

async def run_phase(
    phase: str,
    tasks: List[Dict],
    cfg: Dict,
    tracker: TokenTracker,
    completed_ids: set,
    concurrency: int = 1,
) -> List[Dict[str, Any]]:
    """Run one phase (phase1 or phase2) over all tasks.

    Args:
        concurrency: Number of parallel OpenSpace workers.
                     1 = serial (default, best for skill accumulation).
                     >1 = concurrent (faster, reduced skill accumulation).
    """
    if concurrency <= 1:
        return await _run_phase_serial(phase, tasks, cfg, tracker, completed_ids)
    else:
        return await _run_phase_concurrent(
            phase, tasks, cfg, tracker, completed_ids, concurrency
        )


def _print_task_summary(record: Dict) -> None:
    tokens = record["tokens"]
    skills = record["skills"]
    exe = record["execution"]
    evaluation = record.get("evaluation", {})
    status_icon = "✅" if record["status"] == "success" else "❌"

    agent_prompt = tokens.get('agent_prompt_tokens', tokens['prompt_tokens'])
    agent_comp = tokens.get('agent_completion_tokens', tokens['completion_tokens'])
    agent_total = tokens.get('agent_total_tokens', tokens['total_tokens'])
    overhead = tokens['total_tokens'] - agent_total

    print(f"\n  {status_icon} Status: {record['status']}")
    print(f"  📊 Tokens (total): {tokens['total_tokens']:,} "
          f"(prompt: {tokens['prompt_tokens']:,}, "
          f"completion: {tokens['completion_tokens']:,})")
    print(f"  📊 Tokens (agent): {agent_total:,} "
          f"(prompt: {agent_prompt:,}, "
          f"completion: {agent_comp:,}, "
          f"overhead: {overhead:,})")
    print(f"  💰 Cost: ${tokens['cost_usd']:.4f}")
    print(f"  🔧 Iterations: {exe['iterations']}, "
          f"Tool calls: {exe['tool_calls']}, "
          f"Time: {exe['time_sec']:.1f}s")
    print(f"  🧬 Skills: {skills['before']} → {skills['after']} "
          f"(+{skills['new_this_task']} new)")
    if skills["used"]:
        print(f"  📎 Skills used: {skills['used']}")
    if evaluation.get("has_evaluation"):
        cliff_mark = " ⚠️cliff" if evaluation.get("cliff_applied") else ""
        print(f"  📝 Quality: {evaluation['score_10']}/10 → "
              f"${evaluation['actual_payment']:.2f}"
              f"/{evaluation.get('max_payment', '?')}{cliff_mark}")


# ═══════════════════════════════════════════════════════════════════
# Comparison & Summary
# ═══════════════════════════════════════════════════════════════════

def _pct(a: int, b: int) -> float:
    """Percentage savings: (a - b) / a * 100.  Positive = saved."""
    return (a - b) / a * 100 if a > 0 else 0.0


def build_comparison(cfg: Dict) -> None:
    """Build per-task comparison and aggregate summary from both phases.

    Reports prompt tokens, completion tokens, and total tokens **separately**
    so that the impact of skill-augmented prompts vs. agent output efficiency
    can be analyzed independently.
    """
    rd = _results_dir(cfg)
    p1 = {r["task_id"]: r for r in _load_jsonl(rd / "phase1_results.jsonl")}
    p2 = {r["task_id"]: r for r in _load_jsonl(rd / "phase2_results.jsonl")}

    comparisons = []
    # Accumulators — separate prompt / completion / total
    agg = {k: 0 for k in (
        "p1_prompt", "p1_completion", "p1_total",
        "p2_prompt", "p2_completion", "p2_total",
        "p1_cost", "p2_cost", "p1_calls", "p2_calls",
        "p1_agent_prompt", "p1_agent_completion", "p1_agent_total",
        "p2_agent_prompt", "p2_agent_completion", "p2_agent_total",
    )}
    savings_total = []
    savings_prompt = []
    savings_completion = []
    savings_agent_total = []
    savings_agent_prompt = []
    savings_agent_completion = []

    # Evaluation accumulators
    eval_scores_p1 = []
    eval_scores_p2 = []
    eval_payments_p1 = []
    eval_payments_p2 = []
    eval_cliffed_p1 = 0
    eval_cliffed_p2 = 0
    eval_count = 0

    for tid in p1:
        r1 = p1[tid]
        r2 = p2.get(tid)
        if r2 is None:
            continue

        t1 = r1["tokens"]
        t2 = r2["tokens"]

        sv_total = _pct(t1["total_tokens"], t2["total_tokens"])
        sv_prompt = _pct(t1["prompt_tokens"], t2["prompt_tokens"])
        sv_completion = _pct(t1["completion_tokens"], t2["completion_tokens"])
        sv_cost = _pct(t1["cost_usd"], t2["cost_usd"]) if t1["cost_usd"] > 0 else 0.0

        # Agent-only tokens (fallback to total if not available)
        a1_prompt = t1.get("agent_prompt_tokens", t1["prompt_tokens"])
        a1_comp = t1.get("agent_completion_tokens", t1["completion_tokens"])
        a1_total = t1.get("agent_total_tokens", t1["total_tokens"])
        a2_prompt = t2.get("agent_prompt_tokens", t2["prompt_tokens"])
        a2_comp = t2.get("agent_completion_tokens", t2["completion_tokens"])
        a2_total = t2.get("agent_total_tokens", t2["total_tokens"])
        sv_agent_total = _pct(a1_total, a2_total)
        sv_agent_prompt = _pct(a1_prompt, a2_prompt)
        sv_agent_completion = _pct(a1_comp, a2_comp)

        # ── Evaluation data ──
        e1 = r1.get("evaluation", {})
        e2 = r2.get("evaluation", {})
        eval_comp = {}
        if e1.get("has_evaluation") and e2.get("has_evaluation"):
            eval_comp = {
                "phase1_score": e1["score_10"],
                "phase2_score": e2["score_10"],
                "score_change": round(e2["score_10"] - e1["score_10"], 1),
                "phase1_actual_payment": e1["actual_payment"],
                "phase2_actual_payment": e2["actual_payment"],
                "phase1_cliff_applied": e1.get("cliff_applied", False),
                "phase2_cliff_applied": e2.get("cliff_applied", False),
            }
            eval_scores_p1.append(e1["score_10"])
            eval_scores_p2.append(e2["score_10"])
            eval_payments_p1.append(e1["actual_payment"])
            eval_payments_p2.append(e2["actual_payment"])
            if e1.get("cliff_applied"):
                eval_cliffed_p1 += 1
            if e2.get("cliff_applied"):
                eval_cliffed_p2 += 1
            eval_count += 1

        comp = {
            "task_id": tid,
            "occupation": r1.get("occupation", ""),
            "sector": r1.get("sector", ""),
            # ── Total tokens (all calls) ──
            "phase1_tokens": t1["total_tokens"],
            "phase2_tokens": t2["total_tokens"],
            "token_savings_pct": round(sv_total, 2),
            # ── Prompt tokens (all calls) ──
            "phase1_prompt_tokens": t1["prompt_tokens"],
            "phase2_prompt_tokens": t2["prompt_tokens"],
            "prompt_savings_pct": round(sv_prompt, 2),
            # ── Completion tokens (all calls) ──
            "phase1_completion_tokens": t1["completion_tokens"],
            "phase2_completion_tokens": t2["completion_tokens"],
            "completion_savings_pct": round(sv_completion, 2),
            # ── Agent-only tokens (excludes skill engine overhead) ──
            "phase1_agent_prompt": a1_prompt,
            "phase2_agent_prompt": a2_prompt,
            "agent_prompt_savings_pct": round(sv_agent_prompt, 2),
            "phase1_agent_completion": a1_comp,
            "phase2_agent_completion": a2_comp,
            "agent_completion_savings_pct": round(sv_agent_completion, 2),
            "phase1_agent_total": a1_total,
            "phase2_agent_total": a2_total,
            "agent_total_savings_pct": round(sv_agent_total, 2),
            # ── LLM calls ──
            "phase1_llm_calls": t1["llm_calls"],
            "phase2_llm_calls": t2["llm_calls"],
            # ── Cost ──
            "phase1_cost": round(t1["cost_usd"], 6),
            "phase2_cost": round(t2["cost_usd"], 6),
            "cost_savings_pct": round(sv_cost, 2),
            # ── Execution ──
            "phase1_iterations": r1["execution"]["iterations"],
            "phase2_iterations": r2["execution"]["iterations"],
            "phase1_time": r1["execution"]["time_sec"],
            "phase2_time": r2["execution"]["time_sec"],
            "phase1_status": r1["status"],
            "phase2_status": r2["status"],
            "skills_used_p2": r2["skills"].get("used", []),
            "skills_total_after_p1": r1["skills"]["after"],
            # ── Evaluation (ClawWork-aligned) ──
            **eval_comp,
        }
        comparisons.append(comp)
        _append_jsonl(rd / "comparison.jsonl", comp)

        # Accumulate
        agg["p1_prompt"] += t1["prompt_tokens"]
        agg["p1_completion"] += t1["completion_tokens"]
        agg["p1_total"] += t1["total_tokens"]
        agg["p2_prompt"] += t2["prompt_tokens"]
        agg["p2_completion"] += t2["completion_tokens"]
        agg["p2_total"] += t2["total_tokens"]
        agg["p1_cost"] += t1["cost_usd"]
        agg["p2_cost"] += t2["cost_usd"]
        agg["p1_calls"] += t1["llm_calls"]
        agg["p2_calls"] += t2["llm_calls"]
        agg["p1_agent_prompt"] += a1_prompt
        agg["p1_agent_completion"] += a1_comp
        agg["p1_agent_total"] += a1_total
        agg["p2_agent_prompt"] += a2_prompt
        agg["p2_agent_completion"] += a2_comp
        agg["p2_agent_total"] += a2_total
        savings_total.append(sv_total)
        savings_prompt.append(sv_prompt)
        savings_completion.append(sv_completion)
        savings_agent_total.append(sv_agent_total)
        savings_agent_prompt.append(sv_agent_prompt)
        savings_agent_completion.append(sv_agent_completion)

    # ── Aggregate summary ──
    n = len(comparisons)
    if n == 0:
        print("⚠️  No matching task pairs found for comparison.")
        return

    def _stats(lst: list) -> dict:
        s = sorted(lst)
        return {
            "mean": round(sum(s) / len(s), 2),
            "median": round(s[len(s) // 2], 2),
            "min": round(min(s), 2),
            "max": round(max(s), 2),
        }

    # Skills snapshot after Phase 1
    skills_file = rd / "skills_snapshot.json"
    skills_data = []
    if skills_file.exists():
        with open(skills_file) as f:
            skills_data = json.load(f)

    # ── Evaluation summary ──
    eval_summary = {}
    if eval_count > 0:
        mean_p1 = sum(eval_scores_p1) / eval_count
        mean_p2 = sum(eval_scores_p2) / eval_count
        improved = sum(1 for i in range(eval_count) if eval_scores_p2[i] > eval_scores_p1[i])
        same = sum(1 for i in range(eval_count) if eval_scores_p2[i] == eval_scores_p1[i])
        regressed = sum(1 for i in range(eval_count) if eval_scores_p2[i] < eval_scores_p1[i])
        eval_summary = {
            "tasks_evaluated": eval_count,
            "phase1": {
                "mean_score": round(mean_p1, 2),
                "total_actual_payment": round(sum(eval_payments_p1), 2),
                "cliffed_count": eval_cliffed_p1,
            },
            "phase2": {
                "mean_score": round(mean_p2, 2),
                "total_actual_payment": round(sum(eval_payments_p2), 2),
                "cliffed_count": eval_cliffed_p2,
            },
            "score_change": round(mean_p2 - mean_p1, 2),
            "improved": improved,
            "same": same,
            "regressed": regressed,
            "cliff_threshold": _MIN_EVALUATION_THRESHOLD,
        }

    summary = {
        "run_name": cfg["run_name"],
        "model": cfg["model"],
        "total_tasks": n,
        "timestamp": datetime.now().isoformat(),
        "token_savings": {
            "total": {
                "overall_pct": round(_pct(agg["p1_total"], agg["p2_total"]), 2),
                "per_task": _stats(savings_total),
                "phase1": agg["p1_total"],
                "phase2": agg["p2_total"],
            },
            "prompt": {
                "overall_pct": round(_pct(agg["p1_prompt"], agg["p2_prompt"]), 2),
                "per_task": _stats(savings_prompt),
                "phase1": agg["p1_prompt"],
                "phase2": agg["p2_prompt"],
            },
            "completion": {
                "overall_pct": round(_pct(agg["p1_completion"], agg["p2_completion"]), 2),
                "per_task": _stats(savings_completion),
                "phase1": agg["p1_completion"],
                "phase2": agg["p2_completion"],
            },
        },
        "agent_token_savings": {
            "total": {
                "overall_pct": round(_pct(agg["p1_agent_total"], agg["p2_agent_total"]), 2),
                "per_task": _stats(savings_agent_total),
                "phase1": agg["p1_agent_total"],
                "phase2": agg["p2_agent_total"],
            },
            "prompt": {
                "overall_pct": round(_pct(agg["p1_agent_prompt"], agg["p2_agent_prompt"]), 2),
                "per_task": _stats(savings_agent_prompt),
                "phase1": agg["p1_agent_prompt"],
                "phase2": agg["p2_agent_prompt"],
            },
            "completion": {
                "overall_pct": round(_pct(agg["p1_agent_completion"], agg["p2_agent_completion"]), 2),
                "per_task": _stats(savings_agent_completion),
                "phase1": agg["p1_agent_completion"],
                "phase2": agg["p2_agent_completion"],
            },
        },
        "llm_calls": {
            "phase1": agg["p1_calls"],
            "phase2": agg["p2_calls"],
        },
        "cost_savings": {
            "total_phase1_usd": round(agg["p1_cost"], 4),
            "total_phase2_usd": round(agg["p2_cost"], 4),
            "saved_usd": round(agg["p1_cost"] - agg["p2_cost"], 4),
        },
        "evaluation": eval_summary,
        "skills": {
            "total_after_phase1": len(skills_data),
            "by_origin": _count_skills_by_origin(skills_data),
        },
    }

    with open(rd / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # ── Print summary ──
    ts = summary["token_savings"]
    print("\n" + "=" * 60)
    print("📊 BENCHMARK SUMMARY")
    print("=" * 60)
    print(f"  Tasks compared: {n}")
    print(f"  Model: {cfg['model']}")

    ats = summary.get("agent_token_savings", {})

    print(f"\n  🔑 Token Savings — All Calls (Total = Prompt + Completion):")
    print(f"     {'':20s} {'Overall':>10s} {'Mean':>10s} {'Median':>10s}")
    for label, key in [("Total tokens", "total"),
                       ("↳ Prompt tokens", "prompt"),
                       ("↳ Completion tokens", "completion")]:
        s = ts[key]
        print(f"     {label:<20s} {s['overall_pct']:>+9.1f}% "
              f"{s['per_task']['mean']:>+9.1f}% "
              f"{s['per_task']['median']:>+9.1f}%")

    if ats:
        print(f"\n  🎯 Token Savings — Agent Only (excludes skill engine overhead):")
        print(f"     {'':20s} {'Overall':>10s} {'Mean':>10s} {'Median':>10s}")
        for label, key in [("Total tokens", "total"),
                           ("↳ Prompt tokens", "prompt"),
                           ("↳ Completion tokens", "completion")]:
            s = ats[key]
            print(f"     {label:<20s} {s['overall_pct']:>+9.1f}% "
                  f"{s['per_task']['mean']:>+9.1f}% "
                  f"{s['per_task']['median']:>+9.1f}%")

    print(f"\n     Phase 1: {agg['p1_total']:>10,} total "
          f"({agg['p1_prompt']:,} prompt + {agg['p1_completion']:,} completion)")
    print(f"     Phase 2: {agg['p2_total']:>10,} total "
          f"({agg['p2_prompt']:,} prompt + {agg['p2_completion']:,} completion)")
    if ats:
        print(f"     Phase 1 (agent): {agg['p1_agent_total']:>10,} total "
              f"({agg['p1_agent_prompt']:,} prompt + {agg['p1_agent_completion']:,} completion)")
        print(f"     Phase 2 (agent): {agg['p2_agent_total']:>10,} total "
              f"({agg['p2_agent_prompt']:,} prompt + {agg['p2_agent_completion']:,} completion)")
    print(f"     LLM calls: {agg['p1_calls']} → {agg['p2_calls']}")

    # ── Print evaluation summary ──
    if eval_summary:
        print(f"\n  📝 Quality Evaluation (ClawWork-aligned, cliff={_MIN_EVALUATION_THRESHOLD}):")
        print(f"     Evaluated: {eval_summary['tasks_evaluated']} tasks")
        print(f"     Phase 1: mean {eval_summary['phase1']['mean_score']}/10"
              f" | ${eval_summary['phase1']['total_actual_payment']:.2f} earned"
              f" | {eval_summary['phase1']['cliffed_count']} cliffed")
        print(f"     Phase 2: mean {eval_summary['phase2']['mean_score']}/10"
              f" | ${eval_summary['phase2']['total_actual_payment']:.2f} earned"
              f" | {eval_summary['phase2']['cliffed_count']} cliffed")
        print(f"     Change:  {eval_summary['score_change']:+.1f} pts"
              f" | ↑{eval_summary['improved']} ={eval_summary['same']} ↓{eval_summary['regressed']}")

    print(f"\n  🧬 Skills accumulated: {len(skills_data)}")
    print(f"     By origin: {_count_skills_by_origin(skills_data)}")
    print(f"\n  📁 Results: {rd}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
# DB management
# ═══════════════════════════════════════════════════════════════════

def _wipe_skill_db() -> None:
    """Delete the shared .openspace/openspace.db for a fresh start."""
    db_file = _OPENSPACE_DB_DIR / "openspace.db"
    for f in [db_file, db_file.with_suffix(".db-wal"), db_file.with_suffix(".db-shm")]:
        if f.exists():
            f.unlink()
    print("🗑️  Wiped skill database for fresh start")


def _backup_skill_db(dest: Path) -> None:
    """Copy the current .openspace/openspace.db to dest."""
    db_file = _OPENSPACE_DB_DIR / "openspace.db"
    if db_file.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(db_file), str(dest))
        # Also copy WAL if exists (for consistency)
        wal = db_file.with_suffix(".db-wal")
        if wal.exists():
            shutil.copy2(str(wal), str(dest.with_suffix(".db-wal")))
        print(f"💾 Backed up skill DB to {dest}")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

async def main(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)

    # CLI overrides
    if args.max_tasks is not None:
        cfg["max_tasks"] = args.max_tasks
    if args.per_occupation is not None:
        cfg["per_occupation"] = args.per_occupation
    if args.model:
        cfg["model"] = args.model
    if args.run_name:
        cfg["run_name"] = args.run_name
    if args.clawwork_root:
        cfg["clawwork_root"] = args.clawwork_root
    if getattr(args, "use_clawwork_productivity", False):
        cfg["use_clawwork_productivity"] = True

    # When using ClawWork productivity tools, ensure livebench is importable
    # before OpenSpace.initialize() (ShellSession loads productivity_tools which imports livebench)
    if cfg.get("use_clawwork_productivity"):
        clawwork_root = Path(cfg.get("clawwork_root", "") or "").resolve()
        if clawwork_root.is_dir():
            parent = str(clawwork_root)  # ClawWork repo root; livebench is livebench/
            if parent not in sys.path:
                sys.path.insert(0, parent)
                print(f"📎 ClawWork on sys.path for productivity tools: {parent}")
        else:
            print(f"⚠️  use_clawwork_productivity is True but clawwork_root not found: {clawwork_root}")

    # Fixed task list overrides individual filters
    if args.task_list:
        tl_path = Path(args.task_list)
        if not tl_path.is_absolute():
            # Try CWD first, then relative to this package directory
            if tl_path.exists():
                tl_path = tl_path.resolve()
            else:
                tl_path = (Path(__file__).parent / tl_path).resolve()
        if not tl_path.exists():
            print(f"❌ Task list file not found: {tl_path}")
            print(f"   (also tried relative to CWD and {Path(__file__).parent})")
            return
        with open(tl_path, "r") as f:
            tl_data = json.load(f)
        fixed_ids = tl_data.get("task_ids", [])
        if not fixed_ids:
            print(f"❌ No 'task_ids' array found in {tl_path}")
            return
        cfg["task_ids"] = fixed_ids
        # Disable filters that conflict with the fixed list
        cfg["max_tasks"] = None
        cfg["per_occupation"] = None
        cfg["sectors"] = None
        cfg["occupations"] = None
        print(f"📋 Using fixed task list: {tl_path.name} ({len(fixed_ids)} tasks)")
        if "_selection_principle" in tl_data:
            desc = tl_data["_selection_principle"].get("description", "")
            if desc:
                print(f"   {desc}")

    concurrency = args.concurrency or 1

    # Evaluation toggle
    if args.no_eval:
        cfg["enable_evaluation"] = False
        print("📝 Evaluation disabled (--no-eval)")
    else:
        cfg.setdefault("enable_evaluation", True)

    # Propagate model to sub-agents (ShellAgentTool reads OPENSPACE_MODEL)
    os.environ["OPENSPACE_MODEL"] = cfg.get("model", "")

    # Pre-flight checks
    env_ok = _check_environment(cfg)
    if not env_ok and not args.dry_run:
        print("❌ Environment checks failed. Fix the issues above and retry.")
        print("   Or use --dry-run to just check without executing.")
        return

    # Load tasks (needed by --prefetch-only and --dry-run before results dir)
    tasks = load_tasks(
        clawwork_root=cfg.get("clawwork_root", ""),
        gdpval_path=cfg.get("gdpval_path"),
        task_ids=cfg.get("task_ids"),
        max_tasks=cfg.get("max_tasks"),
        sectors=cfg.get("sectors"),
        occupations=cfg.get("occupations"),
        per_occupation=cfg.get("per_occupation"),
    )

    # ── Prefetch reference files ──
    from gdpval_bench.task_loader import prefetch_reference_files

    if args.prefetch_only:
        print("\n📦 Prefetch-only mode: downloading all reference files …")
        result = prefetch_reference_files(tasks)
        n_tasks_with_files = sum(1 for v in result.values() if v)
        total_files = sum(len(v) for v in result.values())
        print(f"\n✅ Prefetch complete: {total_files} files cached for "
              f"{n_tasks_with_files} tasks.")
        return

    if args.dry_run:
        print(f"\n🏁 Dry run complete. {len(tasks)} tasks ready.")
        mode = f"concurrent ({concurrency} workers)" if concurrency > 1 else "serial"
        print(f"   Mode: {mode}")
        print(f"   To execute: remove --dry-run flag")
        if tasks:
            print(f"\n   Sample task:")
            t = tasks[0]
            print(f"     ID: {t['task_id']}")
            print(f"     Occupation: {t['occupation']}")
            print(f"     Prompt ({len(t['prompt'])} chars): {t['prompt'][:150]}...")
        return

    if not tasks:
        print("❌ No tasks loaded. Check config.")
        return

    if not args.no_prefetch:
        has_refs = sum(1 for t in tasks if t.get("reference_files"))
        if has_refs:
            print(f"\n📦 Auto-prefetching reference files for {has_refs} tasks …")
            print(f"   (use --no-prefetch to skip, --prefetch-only to run separately)")
            prefetch_reference_files(tasks)

    rd = _results_dir(cfg)
    rd.mkdir(parents=True, exist_ok=True)

    # Save config for reproducibility
    run_cfg = {**cfg, "concurrency": concurrency}
    with open(rd / "config.json", "w") as f:
        json.dump(run_cfg, f, indent=2, ensure_ascii=False)

    tracker = TokenTracker(record_details=cfg.get("record_call_details", True))

    mode_str = f"concurrent ({concurrency} workers)" if concurrency > 1 else "serial"
    print(f"\n⚡ Execution mode: {mode_str}")

    # ── Phase 1 ──
    if not args.phase2_only:
        print("\n" + "🔵" * 30)
        print("  PHASE 1: Cold Start — Skills Accumulate")
        if concurrency > 1:
            print(f"  ⚡ Concurrent mode: {concurrency} workers")
            print("  ⚠️  Cross-task skill accumulation reduced (concurrent batches)")
        print("🔵" * 30)

        p1_results_file = rd / "phase1_results.jsonl"
        completed_p1 = _completed_task_ids(p1_results_file) if args.resume else set()

        if not args.resume:
            _wipe_skill_db()

        if completed_p1:
            print(f"📌 Resuming Phase 1: {len(completed_p1)} tasks already done")
        else:
            print(f"🚀 Running {len(tasks)} tasks...")

        await run_phase("phase1", tasks, cfg, tracker, completed_p1, concurrency)

        # Snapshot skills after Phase 1
        try:
            from openspace.skill_engine import SkillStore
            store = SkillStore()
            skills = _snapshot_skills(store)
            with open(rd / "skills_snapshot.json", "w") as f:
                json.dump(skills, f, indent=2, ensure_ascii=False, default=str)
            store.close()
            print(f"\n🧬 Phase 1 complete: {len(skills)} skills accumulated")
        except Exception as e:
            print(f"⚠️  Could not snapshot skills: {e}")

        # Backup the DB before Phase 2
        _backup_skill_db(rd / "phase1_skill_db.sqlite")

    # ── Phase 2 ──
    if not args.phase1_only:
        print("\n" + "🟢" * 30)
        print("  PHASE 2: Full Warm — All Phase 1 Skills Available")
        if concurrency > 1:
            print(f"  ⚡ Concurrent mode: {concurrency} workers")
        print("🟢" * 30)

        p2_results_file = rd / "phase2_results.jsonl"
        completed_p2 = _completed_task_ids(p2_results_file) if args.resume else set()

        if completed_p2:
            print(f"📌 Resuming Phase 2: {len(completed_p2)} tasks already done")
        else:
            print(f"🚀 Running {len(tasks)} tasks with accumulated skills...")

        await run_phase("phase2", tasks, cfg, tracker, completed_p2, concurrency)

        # ── Comparison ──
        print("\n📊 Building comparison...")
        build_comparison(cfg)
    else:
        print("\n📌 Phase 1 only mode — skipping Phase 2.")


def _check_environment(cfg: Dict) -> bool:
    """Pre-flight checks: API keys, dependencies, data availability."""
    print("🔍 Pre-flight checks...")
    ok = True

    # 1. Check LLM API key
    model = cfg.get("model", "")
    if "openrouter" in model:
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            print("  ❌ OPENROUTER_API_KEY not set")
            print("     export OPENROUTER_API_KEY='sk-or-...'")
            ok = False
        else:
            print(f"  ✅ OPENROUTER_API_KEY set ({key[:12]}...)")
    elif "openai" in model or "gpt" in model:
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            print("  ❌ OPENAI_API_KEY not set")
            ok = False
        else:
            print(f"  ✅ OPENAI_API_KEY set")
    elif "anthropic" in model or "claude" in model:
        key = os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            print("  ❌ ANTHROPIC_API_KEY or OPENROUTER_API_KEY not set")
            ok = False
        else:
            print(f"  ✅ API key set for Anthropic model")
    else:
        print(f"  ⚠️  Model: {model} — make sure the corresponding API key is set")

    # 2. Check litellm
    try:
        import litellm
        ver = getattr(litellm, "__version__", getattr(litellm, "version", "unknown"))
        print(f"  ✅ litellm ({ver})")
    except ImportError:
        print("  ❌ litellm not installed — pip install litellm")
        ok = False

    # 3. Check openspace
    try:
        from openspace.tool_layer import OpenSpace
        print(f"  ✅ openspace importable")
    except ImportError as e:
        print(f"  ❌ openspace not importable: {e}")
        print(f"     Run from OpenSpace directory or: pip install -e .")
        ok = False

    # 4. Check data availability (quick peek)
    root = Path(cfg.get("clawwork_root", ""))
    gdp = cfg.get("gdpval_path")
    has_data = False
    if gdp and Path(gdp).exists():
        has_data = True
        print(f"  ✅ GDPVal data at {gdp}")
    elif root.exists():
        for p in [
            root / "gdpval",
            root / "livebench" / "data" / "tasks" / "example_tasks.jsonl",
            root / "scripts" / "task_value_estimates" / "task_values.jsonl",
        ]:
            if p.exists():
                has_data = True
                print(f"  ✅ Task data found: {p}")
                break
    if not has_data:
        try:
            import datasets
            print(f"  ✅ HuggingFace datasets library installed — will auto-download")
            has_data = True
        except ImportError:
            print(f"  ⚠️  No local task data found & no HuggingFace datasets library")
            print(f"     Fix: pip install datasets  OR  set --clawwork-root")

    # 5. Check evaluation readiness
    if cfg.get("enable_evaluation", True):
        eval_key = os.environ.get("EVALUATION_API_KEY") or os.environ.get("OPENAI_API_KEY")
        meta_dir = root / "eval" / "meta_prompts" if root.exists() else None
        if eval_key:
            print(f"  ✅ Evaluation API key set")
        else:
            print(f"  ⚠️  No EVALUATION_API_KEY or OPENAI_API_KEY for evaluation")
            print(f"     Evaluation will fail. Use --no-eval to skip.")
        if meta_dir and meta_dir.exists():
            n_meta = len(list(meta_dir.glob("*.json")))
            print(f"  ✅ Evaluation meta-prompts: {n_meta} rubrics in {meta_dir}")
            print(f"     (If evaluation fails with 'No module named langchain_core', run: pip install -r gdpval_bench/requirements-eval.txt)")
        elif meta_dir:
            print(f"  ⚠️  Meta-prompts dir not found: {meta_dir}")
            print(f"     Evaluation needs ClawWork/eval/meta_prompts/")
    else:
        print(f"  ℹ️  Evaluation disabled")

    print()
    return ok


def cli():
    parser = argparse.ArgumentParser(
        description="GDPVal Benchmark for OpenSpace skill-driven token savings"
    )
    parser.add_argument("--config", type=str, default=str(_DEFAULT_CONFIG),
                        help="Path to config JSON file (default: gdpval_bench/config.json)")
    parser.add_argument("--max-tasks", type=int, default=None,
                        help="Max tasks to run (for testing)")
    parser.add_argument("--per-occupation", type=int, default=None,
                        help="Stratified sampling: pick N tasks per occupation "
                             "(e.g., --per-occupation 1 → 44 tasks covering all occupations)")
    parser.add_argument("--task-list", type=str, default=None,
                        help="Path to a task-list JSON file (e.g. tasks_50.json). "
                             "The file must have a 'task_ids' array. Overrides max-tasks / "
                             "per-occupation / sectors / occupations filters.")
    parser.add_argument("--model", type=str, default=None,
                        help="LLM model override")
    parser.add_argument("--run-name", type=str, default=None,
                        help="Run name (determines output directory)")
    parser.add_argument("--clawwork-root", type=str, default=None,
                        help="Path to ClawWork project (for loading tasks)")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="Number of parallel OpenSpace workers per phase "
                             "(default: 1 = serial). Higher values reduce "
                             "cross-task skill accumulation within a phase.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint")
    parser.add_argument("--phase2-only", action="store_true",
                        help="Skip Phase 1, run Phase 2 only (requires Phase 1 DB)")
    parser.add_argument("--phase1-only", action="store_true",
                        help="Run Phase 1 only, skip Phase 2")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only load tasks and check environment, don't execute")
    parser.add_argument("--no-eval", action="store_true",
                        help="Disable ClawWork-aligned evaluation after each task "
                             "(saves API calls to the evaluation model)")
    parser.add_argument("--prefetch-only", action="store_true",
                        help="Only pre-download all reference files to local cache, "
                             "then exit. Run this first to avoid SSL flakiness "
                             "during benchmark execution.")
    parser.add_argument("--no-prefetch", action="store_true",
                        help="Skip the automatic prefetch step (download on-the-fly instead)")
    parser.add_argument("--use-clawwork-productivity", action="store_true",
                        help="Enable ClawWork productivity tools (search_web, create_file, read_file, etc.) for fair comparison with ClawWork; requires livebench installed.")
    args = parser.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    cli()
