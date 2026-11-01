"""
Task Loader — load GDPVal tasks for benchmarking.

Data resolution order:
  1. GDPVal HuggingFace dataset → auto-download if not cached
  2. Local parquet at ``clawwork_root/gdpval/data/…``
  3. ClawWork example_tasks.jsonl (5 demo tasks with full prompts)
  4. ClawWork task_values.jsonl (220 tasks, summary-only — last resort)

Each task is normalized to:
  {
      "task_id": str,
      "occupation": str,
      "sector": str,
      "prompt": str,
      "reference_files": list,       # relative paths inside HF dataset
      "reference_file_urls": list,    # direct download URLs
      "task_value_usd": float,
      "hourly_wage": float,
      "hours_estimate": float,
  }
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# HuggingFace dataset identifier — public GDPVal dataset
_HF_DATASET = "openai/gdpval"
_HF_CACHE_SUBDIR = "gdpval_cache"


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def load_tasks(
    clawwork_root: str = "",
    gdpval_path: Optional[str] = None,
    task_ids: Optional[List[str]] = None,
    max_tasks: Optional[int] = None,
    sectors: Optional[List[str]] = None,
    occupations: Optional[List[str]] = None,
    per_occupation: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Load GDPVal tasks from the best available source.

    Args:
        clawwork_root: Path to ClawWork project root (for local data files).
                       Empty string → skip local ClawWork data files.
        gdpval_path: Explicit path to a GDPVal parquet file or directory.
        task_ids: Only load tasks with these IDs.
        max_tasks: Max tasks to return (for quick testing).
        sectors: Filter by sector name (case-insensitive substring).
        occupations: Filter by occupation name (case-insensitive substring).
        per_occupation: Stratified sampling — pick N tasks per occupation.
                        Ensures coverage across all 44 occupations.
                        Applied after sector/occupation filters but before max_tasks.

    Returns:
        List of normalized task dicts.
    """
    tasks: List[Dict[str, Any]] = []
    source = "none"

    root = Path(clawwork_root) if clawwork_root else None

    # ── Source 1: Explicit gdpval_path (parquet file or dir) ──
    if gdpval_path:
        pq = _find_parquet(Path(gdpval_path))
        if pq:
            tasks = _load_from_parquet(pq, pq.parent.parent)
            source = f"parquet ({pq})"

    # ── Source 2: HuggingFace auto-download ──
    if not tasks:
        tasks, hf_source = _try_huggingface()
        if tasks:
            source = hf_source

    # ── Source 3: Local parquet under ClawWork/gdpval/ ──
    if not tasks and root:
        pq = _find_parquet(root / "gdpval")
        if pq:
            tasks = _load_from_parquet(pq, root / "gdpval")
            source = f"parquet ({pq})"

    # ── Source 4: example_tasks.jsonl (5 demo tasks, full prompts) ──
    if not tasks and root:
        ex_path = root / "livebench" / "data" / "tasks" / "example_tasks.jsonl"
        if ex_path.exists():
            tasks = _load_from_jsonl(ex_path)
            source = f"example_tasks.jsonl ({ex_path})"
            print(f"   ⚠️  Using 5 demo tasks only — for full 220 tasks, provide GDPVal dataset")

    # ── Source 5: task_values.jsonl (220 tasks, summary only) ──
    if not tasks and root:
        tv_path = root / "scripts" / "task_value_estimates" / "task_values.jsonl"
        if tv_path.exists():
            tasks = _load_from_task_values(tv_path)
            source = f"task_values.jsonl ({tv_path})"
            print(f"   ⚠️  Using task_summary as prompt (short descriptions)")
            print(f"       For full prompts, provide GDPVal parquet or HuggingFace dataset")

    # ── Enrich with pricing data ──
    if tasks and root:
        _enrich_with_pricing(tasks, root)

    # ── No data found ──
    if not tasks:
        tried = []
        if gdpval_path:
            tried.append(f"  • Explicit path: {gdpval_path}")
        tried.append(f"  • HuggingFace: {_HF_DATASET}")
        if root:
            tried.append(f"  • Local parquet: {root / 'gdpval'}")
            tried.append(f"  • example_tasks.jsonl: {root / 'livebench/data/tasks/example_tasks.jsonl'}")
            tried.append(f"  • task_values.jsonl: {root / 'scripts/task_value_estimates/task_values.jsonl'}")
        raise FileNotFoundError(
            "No GDPVal data found. Tried:\n" + "\n".join(tried) + "\n\n"
            "Quick fix options:\n"
            "  1. pip install datasets && python -m gdpval_bench  (auto-downloads from HuggingFace)\n"
            "  2. Set --clawwork-root to your ClawWork directory\n"
        )

    # ── Apply filters ──
    if task_ids:
        id_set = set(task_ids)
        tasks = [t for t in tasks if t["task_id"] in id_set]

    if sectors:
        sectors_lower = [s.lower() for s in sectors]
        tasks = [
            t for t in tasks
            if any(sl in t.get("sector", "").lower() for sl in sectors_lower)
        ]

    if occupations:
        occ_lower = [o.lower() for o in occupations]
        tasks = [
            t for t in tasks
            if any(ol in t.get("occupation", "").lower() for ol in occ_lower)
        ]

    # ── Stratified sampling (N per occupation) ──
    if per_occupation is not None and per_occupation > 0:
        tasks = _stratified_sample(tasks, per_occupation)

    if max_tasks is not None:
        tasks = tasks[:max_tasks]

    # ── Summary ──
    print(f"✅ Loaded {len(tasks)} GDPVal tasks [{source}]")
    if tasks:
        sectors_set = {t.get("sector", "?") for t in tasks}
        occupations_set = {t.get("occupation", "?") for t in tasks}
        print(f"   Sectors: {len(sectors_set)}, Occupations: {len(occupations_set)}")
        avg_prompt_len = sum(len(t.get("prompt", "")) for t in tasks) // len(tasks)
        print(f"   Avg prompt length: {avg_prompt_len} chars")
        values = [t.get("task_value_usd", 0) for t in tasks]
        if any(v > 0 for v in values):
            print(f"   Value range: ${min(v for v in values if v > 0):.2f} – ${max(values):.2f}")

    return tasks


# ═══════════════════════════════════════════════════════════════════
# HuggingFace auto-download
# ═══════════════════════════════════════════════════════════════════

def _try_huggingface() -> tuple:
    """Try to load from HuggingFace datasets library. Returns (tasks, source_desc) or ([], '')."""
    try:
        from datasets import load_dataset
    except ImportError:
        return [], ""

    try:
        print(f"📥 Downloading GDPVal from HuggingFace ({_HF_DATASET})...")
        ds = load_dataset(_HF_DATASET, split="train")
        tasks = []
        for row in ds:
            task = {
                "task_id": str(row.get("task_id", "")),
                "occupation": str(row.get("occupation", "")),
                "sector": str(row.get("sector", "")),
                "prompt": str(row.get("prompt", "")),
                "reference_files": row.get("reference_files", []) or [],
                "reference_file_urls": row.get("reference_file_urls", []) or [],
                "task_value_usd": 0.0,
                "hourly_wage": 0.0,
                "hours_estimate": 0.0,
            }
            tasks.append(task)
        print(f"   [HuggingFace] {len(tasks)} tasks loaded")
        has_refs = sum(1 for t in tasks if t["reference_files"])
        total_refs = sum(len(t["reference_files"]) for t in tasks)
        print(f"   [HuggingFace] {has_refs} tasks have reference files ({total_refs} files total)")
        return tasks, f"HuggingFace ({_HF_DATASET})"
    except Exception as e:
        print(f"   ⚠️  HuggingFace download failed: {e}")
        return [], ""


# ═══════════════════════════════════════════════════════════════════
# Local file loaders
# ═══════════════════════════════════════════════════════════════════

def _find_parquet(path: Path) -> Optional[Path]:
    """Find parquet file — handles both file path and directory."""
    if not path.exists():
        return None
    if path.is_file() and path.suffix == ".parquet":
        return path
    # Directory: look for standard HF layout
    pq = path / "data" / "train-00000-of-00001.parquet"
    if pq.exists():
        return pq
    # Also try direct children
    for f in path.glob("*.parquet"):
        return f
    for f in path.rglob("*.parquet"):
        return f
    return None


def _load_from_parquet(parquet_path: Path, gdpval_dir: Path) -> List[Dict[str, Any]]:
    """Load from GDPVal parquet (has full prompts + reference files)."""
    try:
        import pandas as pd
    except ImportError:
        print("   ⚠️  pandas not installed — cannot read parquet. pip install pandas pyarrow")
        return []

    df = pd.read_parquet(str(parquet_path))
    tasks = []
    for _, row in df.iterrows():
        task = {
            "task_id": str(row.get("task_id", "")),
            "occupation": str(row.get("occupation", "")),
            "sector": str(row.get("sector", "")),
            "prompt": str(row.get("prompt", "")),
            "reference_files": _resolve_references(
                row.get("reference_files", []), gdpval_dir
            ),
            "task_value_usd": 0.0,
            "hourly_wage": 0.0,
            "hours_estimate": 0.0,
        }
        tasks.append(task)

    print(f"   [parquet] {len(tasks)} tasks loaded from {parquet_path}")
    return tasks


def _load_from_jsonl(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Load from example_tasks.jsonl (demo tasks with full prompts)."""
    tasks = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            task = {
                "task_id": rec.get("task_id", ""),
                "occupation": rec.get("occupation", ""),
                "sector": rec.get("sector", ""),
                "prompt": rec.get("prompt", ""),
                "reference_files": rec.get("reference_files", []),
                "task_value_usd": 0.0,
                "hourly_wage": 0.0,
                "hours_estimate": 0.0,
            }
            tasks.append(task)

    print(f"   [example_tasks.jsonl] {len(tasks)} tasks loaded from {jsonl_path}")
    return tasks


def _load_from_task_values(tv_path: Path) -> List[Dict[str, Any]]:
    """Load from ClawWork task_values.jsonl (summary only, no full prompt)."""
    tasks = []
    with open(tv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            task = {
                "task_id": rec.get("task_id", ""),
                "occupation": rec.get("occupation", ""),
                "sector": rec.get("sector", ""),
                "prompt": rec.get("task_summary", ""),
                "reference_files": [],
                "task_value_usd": rec.get("task_value_usd", 0.0),
                "hourly_wage": rec.get("hourly_wage", 0.0),
                "hours_estimate": rec.get("hours_estimate", 0.0),
            }
            tasks.append(task)

    print(f"   [task_values.jsonl] {len(tasks)} tasks loaded from {tv_path}")
    return tasks


# ═══════════════════════════════════════════════════════════════════
# Enrichment & utilities
# ═══════════════════════════════════════════════════════════════════

def _enrich_with_pricing(tasks: List[Dict], clawwork_root: Path) -> None:
    """Merge pricing data from task_values.jsonl into tasks that lack it."""
    tv_path = clawwork_root / "scripts" / "task_value_estimates" / "task_values.jsonl"
    if not tv_path.exists():
        return

    pricing: Dict[str, Dict] = {}
    with open(tv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                pricing[rec["task_id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue

    enriched = 0
    for task in tasks:
        if task.get("task_value_usd", 0) > 0:
            continue
        pr = pricing.get(task["task_id"])
        if pr:
            task["task_value_usd"] = pr.get("task_value_usd", 0.0)
            task["hourly_wage"] = pr.get("hourly_wage", 0.0)
            task["hours_estimate"] = pr.get("hours_estimate", 0.0)
            enriched += 1

    if enriched:
        print(f"   [pricing] Enriched {enriched} tasks with task values")


def _stratified_sample(
    tasks: List[Dict[str, Any]], per_occupation: int
) -> List[Dict[str, Any]]:
    """Pick N tasks per occupation for balanced coverage.

    Groups tasks by ``occupation``, takes the first ``per_occupation`` from
    each group, and returns them sorted by (occupation, task_id).
    """
    from collections import defaultdict

    by_occ: Dict[str, List[Dict]] = defaultdict(list)
    for t in tasks:
        by_occ[t.get("occupation", "unknown")].append(t)

    sampled: List[Dict] = []
    for occ in sorted(by_occ):
        sampled.extend(by_occ[occ][:per_occupation])

    n_occ = len(by_occ)
    print(f"   [stratified] {per_occupation} per occupation × {n_occ} occupations "
          f"→ {len(sampled)} tasks")
    return sampled


def _resolve_references(ref_files: Any, base_dir: Path) -> List[str]:
    """Resolve reference file paths relative to GDPVal dataset dir."""
    if not ref_files:
        return []
    if isinstance(ref_files, str):
        ref_files = [ref_files]
    resolved = []
    for rf in ref_files:
        if isinstance(rf, str):
            full = base_dir / rf
            if full.exists():
                resolved.append(str(full))
            else:
                resolved.append(rf)
    return resolved


# ═══════════════════════════════════════════════════════════════════
# Reference file prefetch & cache
# ═══════════════════════════════════════════════════════════════════

# Default cache directory (sibling to gdpval_bench/)
_REF_CACHE_DIR = Path(__file__).resolve().parent / "ref_cache"


def prefetch_reference_files(
    tasks: List[Dict[str, Any]],
    cache_dir: Optional[str] = None,
    retries: int = 8,
) -> Dict[str, List[str]]:
    """Pre-download ALL reference files for all tasks into a local cache.

    Call this once before the benchmark starts so that individual task
    execution never has to hit the network.

    Args:
        tasks: List of normalized task dicts.
        cache_dir: Directory to cache files.  Defaults to ``gdpval_bench/ref_cache/``.
        retries: Max download attempts per file (default 8 — generous for flaky SSL).

    Returns:
        Dict mapping ``task_id → [list of cached absolute file paths]``.
        Tasks whose files were already cached count as success.
    """
    cache = Path(cache_dir) if cache_dir else _REF_CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    # Collect all unique (rel_path, url) pairs across tasks
    # Use rel_path as the cache key (preserves directory structure)
    file_map: Dict[str, str] = {}   # rel_path → url
    task_files: Dict[str, List[str]] = {}  # task_id → [rel_path, …]

    for task in tasks:
        tid = task.get("task_id", "")
        ref_files = task.get("reference_files", []) or []
        ref_urls = task.get("reference_file_urls", []) or []
        paths_for_task: List[str] = []
        for i, rel_path in enumerate(ref_files):
            url = ref_urls[i] if i < len(ref_urls) else None
            if not url:
                url = f"https://huggingface.co/datasets/openai/gdpval/resolve/main/{rel_path}"
                url = url.replace(" ", "%20")
            file_map[rel_path] = url
            paths_for_task.append(rel_path)
        task_files[tid] = paths_for_task

    if not file_map:
        print("📦 No reference files to prefetch.")
        return {}

    total = len(file_map)
    already = 0
    downloaded = 0
    failed_list: List[str] = []

    print(f"\n📦 Prefetching {total} unique reference files into {cache} …")

    for idx, (rel_path, url) in enumerate(file_map.items(), 1):
        filename = Path(rel_path).name
        # Cache with subdirectory (hash) to avoid name collisions
        dest = cache / rel_path
        if dest.exists() and dest.stat().st_size > 0:
            already += 1
            continue

        print(f"  [{idx}/{total}] Downloading {filename} …", end="", flush=True)
        try:
            _download_file(url, dest, retries=retries)
            size_kb = dest.stat().st_size / 1024
            print(f"  ✅ {size_kb:.0f} KB")
            downloaded += 1
        except Exception as e:
            print(f"  ❌ {e}")
            failed_list.append(filename)
            logger.error(f"Prefetch failed: {filename} from {url}: {e}")

    print(f"\n📦 Prefetch complete: {already} cached, {downloaded} downloaded, "
          f"{len(failed_list)} failed")
    if failed_list:
        print(f"  ⚠️  Failed files: {failed_list}")
        logger.warning(f"Prefetch failures: {failed_list}")

    # Build result mapping
    result: Dict[str, List[str]] = {}
    for tid, paths in task_files.items():
        cached_paths = []
        for rp in paths:
            fp = cache / rp
            if fp.exists():
                cached_paths.append(str(fp))
        result[tid] = cached_paths

    return result


# ═══════════════════════════════════════════════════════════════════
# Reference file download & prompt augmentation
# ═══════════════════════════════════════════════════════════════════

def prepare_task_workspace(task: Dict[str, Any], workspace_dir: str) -> str:
    """Download reference files and return the augmented prompt.

    First checks the local prefetch cache (``ref_cache/``); only falls back
    to network download if a file is not cached.  Downloads reference files
    from HuggingFace URLs into ``workspace_dir`` and prepends a section to
    the prompt telling the agent where the files are.

    Args:
        task: Normalized task dict (must contain ``prompt``, ``reference_files``,
              and optionally ``reference_file_urls``).
        workspace_dir: Absolute path to the task's workspace directory.

    Returns:
        Augmented prompt string with file location info prepended.
    """
    import shutil as _shutil

    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    ref_files = task.get("reference_files", []) or []
    ref_urls = task.get("reference_file_urls", []) or []
    original_prompt = task.get("prompt", "")

    if not ref_files:
        return original_prompt

    cache = _REF_CACHE_DIR

    # ── Download / copy files ──
    downloaded: List[str] = []
    failed: List[str] = []

    for i, rel_path in enumerate(ref_files):
        # Extract just the filename (drop the reference_files/hash/ prefix)
        filename = Path(rel_path).name
        dest = ws / filename

        # Already in workspace
        if dest.exists():
            downloaded.append(filename)
            continue

        # Try local cache first
        cached = cache / rel_path
        if cached.exists() and cached.stat().st_size > 0:
            _shutil.copy2(str(cached), str(dest))
            downloaded.append(filename)
            logger.info(f"Copied from cache: {filename}")
            continue

        # Fallback: download from network
        url = ref_urls[i] if i < len(ref_urls) else None
        if not url:
            url = f"https://huggingface.co/datasets/openai/gdpval/resolve/main/{rel_path}"
            url = url.replace(" ", "%20")

        try:
            _download_file(url, dest, retries=5)
            downloaded.append(filename)
            logger.info(f"Downloaded reference file: {filename} ({dest.stat().st_size} bytes)")
            # Also save to cache for next time
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(str(dest), str(cached))
            except OSError:
                pass
        except Exception as e:
            failed.append(f"{filename} ({e})")
            logger.error(f"Failed to download reference file: {filename} from {url}: {e}")

    if downloaded:
        msg = f"Downloaded {len(downloaded)} reference file(s) to {workspace_dir}"
        print(f"  📎 {msg}")
        logger.info(msg)
    if failed:
        msg = f"Failed to download {len(failed)} file(s): {failed[:3]}"
        print(f"  ⚠️  {msg}")
        logger.warning(msg)

    # ── Augment prompt ──
    if not downloaded:
        return original_prompt

    file_list = "\n".join(f"  - {f}" for f in downloaded)
    augmented = (
        f"[REFERENCE FILES]\n"
        f"The following reference files mentioned in this task have been placed "
        f"in your current working directory ({workspace_dir}):\n"
        f"{file_list}\n\n"
        f"You can read and process these files directly from your working directory.\n\n"
        f"[TASK]\n{original_prompt}"
    )
    return augmented


def _download_file(url: str, dest: Path, timeout: int = 60, retries: int = 5) -> None:
    """Download a single file from URL to dest path, with retries.

    Strategy order:
      1. ``curl``  — bypasses Python SSL stack entirely (most robust).
      2. ``wget``  — same benefit, second choice.
      3. ``requests`` — Python-level, good retry support.
      4. ``urllib`` — last resort with relaxed SSL context.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # ---------- Strategy 1: curl (bypasses Python SSL) ----------
    if shutil.which("curl"):
        try:
            _download_via_curl(url, dest, timeout=timeout, retries=retries)
            return
        except Exception as e:
            logger.warning(f"curl download failed for {dest.name}: {e}")
            # Clean up and try next strategy
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass

    # ---------- Strategy 2: wget ----------
    if shutil.which("wget"):
        try:
            _download_via_wget(url, dest, timeout=timeout, retries=retries)
            return
        except Exception as e:
            logger.warning(f"wget download failed for {dest.name}: {e}")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass

    # ---------- Strategy 3: requests ----------
    try:
        import requests as _requests
        _download_via_requests(url, dest, timeout=timeout, retries=retries)
        return
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"requests download failed for {dest.name}: {e}")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass

    # ---------- Strategy 4: urllib (last resort) ----------
    _download_via_urllib(url, dest, timeout=timeout, retries=retries)


def _download_via_curl(url: str, dest: Path, timeout: int = 60, retries: int = 5) -> None:
    """Download using curl subprocess — bypasses Python SSL entirely."""
    cmd = [
        "curl",
        "-fSL",                     # fail on HTTP errors, show errors, follow redirects
        "--retry", str(retries),
        "--retry-delay", "3",
        "--retry-all-errors",       # retry on connection errors too, not just HTTP
        "--connect-timeout", "30",
        # No --max-time: large files (zips, videos) need unlimited transfer time.
        # Instead use --speed-limit/--speed-time to abort only if truly stalled.
        "--speed-limit", "1024",    # abort if speed drops below 1 KB/s …
        "--speed-time", "30",       # … for 30 consecutive seconds
        "-o", str(dest),
        "-H", "User-Agent: gdpval-bench/1.0",
        url,
    ]
    logger.info(f"curl: downloading {dest.name}")
    # No Python-side timeout — let curl manage its own timeouts
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()[-200:] if result.stderr else "unknown error"
        raise RuntimeError(f"curl exited {result.returncode}: {stderr}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("curl produced empty file")


def _download_via_wget(url: str, dest: Path, timeout: int = 60, retries: int = 5) -> None:
    """Download using wget subprocess — bypasses Python SSL entirely."""
    cmd = [
        "wget",
        "-q",                           # quiet
        "--tries", str(retries),
        "--timeout", str(timeout),
        "--wait", "3",
        "--waitretry", "5",
        "--no-dns-cache",
        "-O", str(dest),
        "--header", "User-Agent: gdpval-bench/1.0",
        url,
    ]
    logger.info(f"wget: downloading {dest.name}")
    # Give wget plenty of time (retries × timeout + margin)
    proc_timeout = retries * timeout * 2
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=proc_timeout)
    if result.returncode != 0:
        stderr = result.stderr.strip()[-200:] if result.stderr else "unknown error"
        raise RuntimeError(f"wget exited {result.returncode}: {stderr}")
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("wget produced empty file")


def _download_via_requests(url: str, dest: Path, timeout: int = 60, retries: int = 5) -> None:
    """Download using requests library with retry adapter."""
    import requests  # noqa: F811
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=retries,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "gdpval-bench/1.0"})

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return
        except Exception as e:
            last_error = e
            logger.warning(f"requests attempt {attempt}/{retries} failed for {dest.name}: {e}")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_error  # type: ignore[misc]


def _download_via_urllib(url: str, dest: Path, timeout: int = 60, retries: int = 5) -> None:
    """Download using urllib with relaxed SSL context (last resort)."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    handler = urllib.request.HTTPSHandler(context=ctx)

    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({
            "https": proxy,
            "http": os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or proxy,
        })
        opener = urllib.request.build_opener(proxy_handler, handler)
    else:
        opener = urllib.request.build_opener(handler)

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gdpval-bench/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            return
        except Exception as e:
            last_error = e
            logger.warning(f"urllib attempt {attempt}/{retries} failed for {dest.name}: {e}")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_error  # type: ignore[misc]
