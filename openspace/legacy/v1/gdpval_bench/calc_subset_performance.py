#!/usr/bin/env python3
"""
Two reporting calibers:
  1. Income  = sum(actual_payment)  — pure work revenue
  2. Balance = $10 initial + Income - Token cost  — net balance (ClawWork leaderboard sort key)

Token pricing per model (from ClawWork configs):
  Qwen3.5-Plus:  input $0.12/1M, output $0.69/1M
  Qwen3-Max:     input $0.35/1M, output $1.41/1M
  GLM-4.7:       input $0.40/1M, output $1.50/1M
  ATIC+Qwen/DS:  input $0.50/1M, output $1.50/1M
  Kimi-K2.5:     input $0.50/1M, output $2.80/1M
  Gemini 3.1 Pro:input $2.00/1M, output $12.00/1M
  Claude 4.6:    input $3.00/1M, output $15.00/1M

OpenSpace uses qwen3.5-plus → same pricing as ClawWork's Qwen3.5-Plus agent.

Usage:
    python -m gdpval_bench.calc_subset_performance
"""
from __future__ import annotations

import json
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
CLAWWORK_ROOT = BENCH_DIR.parent.parent / "ClawWork"
AGENT_DATA = CLAWWORK_ROOT / "livebench" / "data" / "agent_data"
RUN_NAME = "qwen3.5-plus-02-15_20260316_010921"

INITIAL_BALANCE = 10.0

# Agents on the ClawWork leaderboard
LEADERBOARD_AGENTS = {
    "ATIC + Qwen3.5-Plus",
    "ATIC-DEEPSEEK",
    "GLM-4.7-test-openrouter-10dollar-1",
    "Gemini 3.1 Pro Preview",
    "Qwen3.5-Plus",
    "kimi-k2.5-test-openrouter-10dollar-1",
    "qwen3-max-10dollar-1",
}

# Display names matching ClawWork leaderboard
DISPLAY_NAMES = {
    "ATIC + Qwen3.5-Plus":                  "ATIC + Qwen3.5-Plus",
    "Gemini 3.1 Pro Preview":               "Gemini 3.1 Pro Preview",
    "Qwen3.5-Plus":                         "Qwen3.5-Plus",
    "GLM-4.7-test-openrouter-10dollar-1":   "GLM-4.7",
    "ATIC-DEEPSEEK":                        "ATIC-DEEPSEEK",
    "qwen3-max-10dollar-1":                 "Qwen3-Max",
    "kimi-k2.5-test-openrouter-10dollar-1": "Kimi-K2.5",
}

# Per-agent token pricing from ClawWork configs (input_per_1m, output_per_1m)
AGENT_PRICING = {
    "Qwen3.5-Plus":                          (0.12, 0.69),
    "qwen3-max-10dollar-1":                  (0.35, 1.41),
    "GLM-4.7-test-openrouter-10dollar-1":    (0.40, 1.50),
    "ATIC + Qwen3.5-Plus":                   (0.50, 1.50),
    "ATIC-DEEPSEEK":                         (0.50, 1.50),
    "kimi-k2.5-test-openrouter-10dollar-1":  (0.50, 2.80),
    "Gemini 3.1 Pro Preview":                (2.00, 12.00),
}
# OpenSpace uses qwen3.5-plus, same pricing
CS_INPUT_PER_1M = 0.12
CS_OUTPUT_PER_1M = 0.69


def dn(name: str) -> str:
    """Get display name for an agent."""
    return DISPLAY_NAMES.get(name, name)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _bar(ratio: float, width: int = 40) -> str:
    filled = max(0, min(width, int(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def calc_token_cost(prompt_tokens: int, completion_tokens: int,
                    input_per_1m: float = CS_INPUT_PER_1M,
                    output_per_1m: float = CS_OUTPUT_PER_1M) -> float:
    return (prompt_tokens / 1_000_000) * input_per_1m + \
           (completion_tokens / 1_000_000) * output_per_1m


def _calc_agent_subset_token_cost(agent_dir: Path, task_ids: set, agent: str) -> float:
    """Calculate token cost for subset tasks from token_costs.jsonl per-task records."""
    tc_file = agent_dir / "economic" / "token_costs.jsonl"
    if not tc_file.exists():
        return 0.0

    cost_by_tid: dict[str, float] = {}
    with open(tc_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            tid = rec.get("task_id", "")
            if tid not in task_ids:
                continue
            # Per-task cost records have llm_usage or session_cost
            llm = rec.get("llm_usage", {})
            task_cost = llm.get("total_cost", 0)
            if task_cost > 0:
                cost_by_tid[tid] = cost_by_tid.get(tid, 0) + task_cost

    if cost_by_tid:
        return sum(cost_by_tid.values())

    # Fallback: pro-rate from full balance if no per-task records found
    bal_file = agent_dir / "economic" / "balance.jsonl"
    if not bal_file.exists():
        return 0.0
    bal_records = load_jsonl(bal_file)
    if not bal_records:
        return 0.0
    total_cost = bal_records[-1].get("total_token_cost", 0)
    # Count total unique tasks in task_completions
    tc_records = load_jsonl(agent_dir / "economic" / "task_completions.jsonl")
    all_tids = set(r.get("task_id") for r in tc_records)
    subset_count = len(task_ids & all_tids)
    total_count = len(all_tids)
    if total_count > 0 and total_cost > 0:
        return total_cost * (subset_count / total_count)
    return 0.0


def main():
    cs_path = RESULTS_DIR / RUN_NAME / "phase1_results.jsonl"
    cs_records = load_jsonl(cs_path)
    if not cs_records:
        print(f"No results at {cs_path}")
        return

    task_ids = set(r["task_id"] for r in cs_records)
    n = len(task_ids)

    # OpenSpace per-task lookups (for common-task comparison)
    cs_pay_by_tid = {}
    cs_score_by_tid = {}
    cs_value_by_tid = {}
    for r in cs_records:
        tid = r["task_id"]
        cs_pay_by_tid[tid] = r.get("evaluation", {}).get("actual_payment", 0)
        cs_score_by_tid[tid] = r.get("evaluation", {}).get("evaluation_score", 0)
        cs_value_by_tid[tid] = r.get("task_value_usd", 0)

    cs_earned = sum(cs_pay_by_tid.values())
    cs_scores = list(cs_score_by_tid.values())
    cs_avg_q = sum(cs_scores) / len(cs_scores)
    cs_total_value = sum(cs_value_by_tid.values())

    # OpenSpace token cost using same pricing as ClawWork's Qwen3.5-Plus (agent tokens only, excl eval)
    cs_agent_prompt = sum(r.get("tokens", {}).get("agent_prompt_tokens", 0) for r in cs_records)
    cs_agent_completion = sum(r.get("tokens", {}).get("agent_completion_tokens", 0) for r in cs_records)
    cs_total_prompt = sum(r.get("tokens", {}).get("prompt_tokens", 0) for r in cs_records)
    cs_total_completion = sum(r.get("tokens", {}).get("completion_tokens", 0) for r in cs_records)
    cs_token_cost = calc_token_cost(cs_agent_prompt, cs_agent_completion, CS_INPUT_PER_1M, CS_OUTPUT_PER_1M)
    cs_balance = INITIAL_BALANCE + cs_earned - cs_token_cost

    p2_path = RESULTS_DIR / RUN_NAME / "phase2_results.jsonl"
    p2_records = load_jsonl(p2_path)
    p2_pay_by_tid = {}
    p2_score_by_tid = {}
    p2_value_by_tid = {}
    for r in p2_records:
        tid = r["task_id"]
        ev = r.get("evaluation", {})
        p2_pay_by_tid[tid] = ev.get("actual_payment", 0)
        if ev.get("has_evaluation") and ev.get("evaluation_score", -1) >= 0:
            p2_score_by_tid[tid] = ev.get("evaluation_score", 0)
        p2_value_by_tid[tid] = r.get("task_value_usd", 0)

    p2_tids = set(r["task_id"] for r in p2_records)
    p2_n = len(p2_tids)
    p2_earned = sum(p2_pay_by_tid.values())
    p2_scores = list(p2_score_by_tid.values())
    p2_avg_q = sum(p2_scores) / len(p2_scores) if p2_scores else 0
    p2_total_value = sum(p2_value_by_tid.values())
    p2_agent_prompt = sum(r.get("tokens", {}).get("agent_prompt_tokens", 0) for r in p2_records)
    p2_agent_completion = sum(r.get("tokens", {}).get("agent_completion_tokens", 0) for r in p2_records)
    p2_token_cost = calc_token_cost(p2_agent_prompt, p2_agent_completion, CS_INPUT_PER_1M, CS_OUTPUT_PER_1M)
    p2_balance = INITIAL_BALANCE + p2_earned - p2_token_cost

    print(f"OpenSpace run: {RUN_NAME}")
    print(f"Phase1: {n} tasks (all evaluated), Task Value ${cs_total_value:,.2f}")
    print(f"Phase2: {p2_n} tasks ({len(p2_scores)} evaluated), Task Value ${p2_total_value:,.2f}")
    print()

    agents = sorted(d.name for d in AGENT_DATA.iterdir()
                    if d.is_dir() and d.name in LEADERBOARD_AGENTS)
    rows = []

    for agent in agents:
        agent_dir = AGENT_DATA / agent

        # Assigned tasks: unique task_ids in tasks.jsonl that overlap with our 50
        tasks_all = load_jsonl(agent_dir / "work" / "tasks.jsonl")
        assigned = set(r.get("task_id") for r in tasks_all) & task_ids

        # Scores from evaluations.jsonl (best score per task_id)
        evals = load_jsonl(agent_dir / "work" / "evaluations.jsonl")
        eval_by_tid: dict[str, float] = {}
        for e in evals:
            tid = e.get("task_id", "")
            s = e.get("evaluation_score")
            if s is not None and tid in task_ids:
                eval_by_tid[tid] = max(eval_by_tid.get(tid, -1), s)

        # Income: from task_completions.jsonl (best money_earned per task_id)
        tc = load_jsonl(agent_dir / "economic" / "task_completions.jsonl")
        earn_by_tid: dict[str, float] = {}
        for t in tc:
            tid = t.get("task_id", "")
            if tid in task_ids:
                earn_by_tid[tid] = max(earn_by_tid.get(tid, 0), t.get("money_earned", 0))

        earned = sum(earn_by_tid.values())

        # Avg Quality A: only evaluated tasks (ClawWork leaderboard definition)
        score_vals_eval = list(eval_by_tid.values())
        avg_q_eval = sum(score_vals_eval) / len(score_vals_eval) if score_vals_eval else 0

        # Avg Quality B: all assigned tasks (unfinished/unevaluated = score 0)
        score_vals_assigned = [eval_by_tid.get(tid, 0.0) for tid in assigned]
        avg_q_assigned = sum(score_vals_assigned) / len(score_vals_assigned) if score_vals_assigned else 0

        # Token cost: read per-task records from token_costs.jsonl for the 50 tasks
        token_cost = _calc_agent_subset_token_cost(agent_dir, task_ids, agent)
        balance = INITIAL_BALANCE + earned - token_cost

        # Common-task comparison: only tasks assigned to BOTH OpenSpace and this agent
        common = assigned  # OpenSpace has all 50, so intersection = agent's assigned
        cs_earn_common = sum(cs_pay_by_tid.get(tid, 0) for tid in common)
        cs_value_common = sum(cs_value_by_tid.get(tid, 0) for tid in common)
        cw_earn_common = sum(earn_by_tid.get(tid, 0) for tid in common)

        # AvgQ on common tasks
        cs_scores_common = [cs_score_by_tid[tid] for tid in common]
        cs_avgq_common = sum(cs_scores_common) / len(cs_scores_common) if cs_scores_common else 0

        cw_scores_eval_common = [eval_by_tid[tid] for tid in common if tid in eval_by_tid]
        cw_avgq_eval_common = sum(cw_scores_eval_common) / len(cw_scores_eval_common) if cw_scores_eval_common else 0

        cw_scores_asgn_common = [eval_by_tid.get(tid, 0.0) for tid in common]
        cw_avgq_asgn_common = sum(cw_scores_asgn_common) / len(cw_scores_asgn_common) if cw_scores_asgn_common else 0

        rows.append({
            "name": agent,
            "earned": earned,
            "avg_q_eval": avg_q_eval,
            "avg_q_assigned": avg_q_assigned,
            "scored": len(score_vals_eval),
            "assigned": len(assigned),
            "completions": len(earn_by_tid),
            "token_cost": token_cost,
            "balance": balance,
            "common_n": len(common),
            "common_tids": common,
            "common_value": cs_value_common,
            "common_cs_earn": cs_earn_common,
            "common_cw_earn": cw_earn_common,
            "common_cs_avgq": cs_avgq_common,
            "common_cw_avgq_eval": cw_avgq_eval_common,
            "common_cw_avgq_asgn": cw_avgq_asgn_common,
            "common_cw_scored": len(cw_scores_eval_common),
            "is_openspace": False,
            "task_count": n,
        })

    rows.append({
        "name": "OpenSpace Phase1",
        "earned": cs_earned,
        "avg_q_eval": cs_avg_q,
        "avg_q_assigned": cs_avg_q,
        "scored": n,
        "assigned": n,
        "completions": n,
        "token_cost": cs_token_cost,
        "balance": cs_balance,
        "common_n": n,
        "common_value": cs_total_value,
        "common_cs_earn": cs_earned,
        "common_cw_earn": cs_earned,
        "is_openspace": True,
        "task_count": n,
    })

    if p2_records:
        rows.append({
            "name": "OpenSpace Phase2",
            "earned": p2_earned,
            "avg_q_eval": p2_avg_q,
            "avg_q_assigned": p2_avg_q,
            "scored": len(p2_scores),
            "assigned": p2_n,
            "completions": p2_n,
            "token_cost": p2_token_cost,
            "balance": p2_balance,
            "common_n": p2_n,
            "common_value": p2_total_value,
            "common_cs_earn": p2_earned,
            "common_cw_earn": p2_earned,
            "is_openspace": True,
            "task_count": p2_n,
        })

    cs_total_tokens = sum(r.get("tokens", {}).get("total_tokens", 0) for r in cs_records)
    cs_agent_tokens = cs_agent_prompt + cs_agent_completion
    p2_total_tokens = sum(r.get("tokens", {}).get("total_tokens", 0) for r in p2_records)
    p2_agent_tokens = p2_agent_prompt + p2_agent_completion

    # Read model from config
    cfg_path = RESULTS_DIR / RUN_NAME / "config.json"
    cs_model = "qwen3.5-plus-02-15"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        cs_model = cfg.get("model", cs_model).split("/")[-1]

    W = 26  # agent name column width

    # Filter out ATIC agents (not full 50-task coverage) from table 1
    t1_rows = [r for r in rows if r["name"] not in ("ATIC + Qwen3.5-Plus", "ATIC-DEEPSEEK")]
    t1_rows.sort(key=lambda x: -x["earned"])

    # ═══════════════════════════════════════════════════════
    # Table 1: Leaderboard
    # ═══════════════════════════════════════════════════════
    print("=" * 115)
    print(f"  Table 1: Leaderboard (Task Value ${cs_total_value:,.2f} for 50 tasks)")
    print(f"  Balance = $10 initial + Income - Token Cost")
    print("=" * 115)
    print()
    print(f"  {'#':>2} {'Agent':{W}} {'Tasks':>5} {'Income':>11} {'Balance':>11} {'TkCost':>7} {'Capture':>8} │ {'Avg Quality':>11} {'Evaluated':>10}")
    print("─" * 110)

    for i, r in enumerate(t1_rows):
        tc = r.get("task_count", r["assigned"])
        tv = r.get("common_value", cs_total_value)
        cap = r["earned"] / tv * 100 if tv else 0
        marker = " ◀◀◀" if r.get("is_openspace") else ""
        aq = f"{r['avg_q_eval']*100:.1f}%" if r["scored"] else "—"
        print(f"  {i+1:>2} {dn(r['name']):{W}} {tc:>5} ${r['earned']:>9,.2f} ${r['balance']:>9,.2f} ${r['token_cost']:>5,.2f} {cap:>6.1f}%"
              f" │ {aq:>11} {r['scored']:>5}/{tc}{marker}")

    print("─" * 110)

    # ── Token usage note under table 1 ──
    print()
    print(f"  OpenSpace model: {cs_model}")
    tok_save = (1 - p2_total_tokens / cs_total_tokens) * 100 if cs_total_tokens else 0
    ag_save = (1 - p2_agent_tokens / cs_agent_tokens) * 100 if cs_agent_tokens else 0
    tpd_p1 = cs_total_tokens / cs_earned if cs_earned else 0
    tpd_p2 = p2_total_tokens / p2_earned if p2_earned else 0
    print(f"  {'':2s} {'':26s} {'Phase 1':>14s} {'Phase 2':>14s} {'Savings':>9s}")
    print(f"  {'':2s} {'Total tokens':26s} {cs_total_tokens:>14,} {p2_total_tokens:>14,} {tok_save:>+8.1f}%")
    print(f"  {'':2s} {'Agent tokens':26s} {cs_agent_tokens:>14,} {p2_agent_tokens:>14,} {ag_save:>+8.1f}%")
    print(f"  {'':2s} {'Tokens / $ earned':26s} {tpd_p1:>14,.0f} {tpd_p2:>14,.0f}")
    print(f"  {'':2s} {'Token cost ($)':26s} {cs_token_cost:>14,.2f} {p2_token_cost:>14,.2f}")
    print()

    # ═══════════════════════════════════════════════════════
    # Table 2: Head-to-head on common tasks (apple-to-apple)
    #   Now includes both Phase 1 and Phase 2 OpenSpace results
    # ═══════════════════════════════════════════════════════
    cw_rows = [r for r in rows if not r.get("is_openspace")]

    # Build Phase 2 per-task lookup for common-task comparison
    p2_pay_tid = {r["task_id"]: r.get("evaluation", {}).get("actual_payment", 0) for r in p2_records}
    p2_score_tid = {}
    for r in p2_records:
        ev = r.get("evaluation", {})
        if ev.get("has_evaluation") and ev.get("evaluation_score", -1) >= 0:
            p2_score_tid[r["task_id"]] = ev.get("evaluation_score", 0)

    for r in cw_rows:
        common = r.get("common_tids", task_ids)
        r["common_p2_earn"] = sum(p2_pay_tid.get(tid, 0) for tid in common)
        p2_sc_common = [p2_score_tid[tid] for tid in common if tid in p2_score_tid]
        r["common_p2_avgq"] = sum(p2_sc_common) / len(p2_sc_common) if p2_sc_common else 0
        r["common_p2_cap"] = r["common_p2_earn"] / r["common_value"] * 100 if r.get("common_value") else 0

    cw_rows.sort(key=lambda x: -(x["common_cs_earn"] - x["common_cw_earn"]))

    print("=" * 165)
    print(f"  Table 2: Head-to-Head on Common Tasks  (CS model: {cs_model})")
    print("=" * 165)
    print()
    hdr_cs = "── OpenSpace (P1 │ P2) ──"
    hdr_cw = "── ClawWork Agent ──"
    print(f"  {'Agent':{W}} {'Tasks':>5} │"
          f" {'P1 Inc':>9} {'P2 Inc':>9} {'CW Inc':>9} {'P2/CW':>6} │"
          f" {'P1 Cap':>7} {'P2 Cap':>7} {'CW Cap':>7} │"
          f" {'P1 AvgQ':>8} {'P2 AvgQ':>8} {'CW(eval)':>9} {'CW(all)':>8} {'CW Eval':>8}")
    print("─" * 160)

    for r in cw_rows:
        cn = r["common_n"]
        cv = r["common_value"]
        cs_e = r["common_cs_earn"]
        p2_e = r["common_p2_earn"]
        cw_e = r["common_cw_earn"]
        ratio_p2 = p2_e / cw_e if cw_e > 0 else float('inf')
        ratio_str = f"{ratio_p2:.1f}x" if cw_e > 0 else "∞"
        cs_cap = cs_e / cv * 100 if cv > 0 else 0
        p2_cap = r["common_p2_cap"]
        cw_cap = cw_e / cv * 100 if cv > 0 else 0

        print(f"  {dn(r['name']):{W}} {cn:>5} │"
              f" ${cs_e:>8,.0f} ${p2_e:>8,.0f} ${cw_e:>8,.0f} {ratio_str:>6} │"
              f" {cs_cap:>6.1f}% {p2_cap:>6.1f}% {cw_cap:>6.1f}% │"
              f" {r['common_cs_avgq']*100:>7.1f}% {r['common_p2_avgq']*100:>7.1f}%"
              f" {r['common_cw_avgq_eval']*100:>7.1f}%  {r['common_cw_avgq_asgn']*100:>7.1f}%"
              f" {r['common_cw_scored']:>4}/{cn}")

    print("─" * 160)
    print()
    print(f"  P1 = Phase 1 (cold start, {cs_model})")
    print(f"  P2 = Phase 2 (warm start with {cs_agent_tokens:,} → {p2_agent_tokens:,} agent tokens, {ag_save:+.0f}% savings)")
    print("  Capture  = Income / Task Value")
    print("  CW(eval) = Agent mean(score) on evaluated tasks only")
    print("  CW(all)  = Agent mean(score) on all shared tasks (unevaluated = 0)")
    print()


if __name__ == "__main__":
    main()
