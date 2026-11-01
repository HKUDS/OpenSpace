---
name: experiment-guide
description: Choose experiments and use their results when investigating program improvements with the portfolio research tools.
---

# Try changes and inspect results

- `experiment`: Test a prediction or uncertainty derived from the research hypothesis, or check a local repair. Describe the mechanism, comparison and how different outcomes would change the design. Use the related T track. The worker can implement and test a change within this experiment; request whole-task A runs only when useful. No code or fixed report format is required.
- `build`: Commit a supported candidate to implementation and the full B comparison. Reuse retained experiment edits with `from_experiment`; no second evaluation request is needed.
- `evaluate`: Run B for an existing candidate without rebuilding it.
- `read_experiment`: Inspect results to decide what to investigate, revise or retain.
- `note`: Retain a provisional mechanism question and what could refute it using track="new", or extend an existing line. When evidence changes the judgment, update the same T track with the finding, supporting X tickets, limits and next uncertainty. These notes reach later research and related workers.
- `drop`: Stop investing in a direction.
- `close`: Finish the current conversation.

Reads and experiments may be interleaved; independent experiments can run together. After submission, continue independent investigation while work runs. To wait for results, send a nonempty assistant note without tool calls; the controller wakes you on a completed result. No polling is needed. Build still commits to B.

After a result, check whether the tested behavior actually occurred and whether the comparison distinguishes the explanations. Revise or narrow the idea when counterevidence warrants it; an inconclusive experiment leaves the question open. If a prototype runs or a second solver answers a task, report that as feasibility evidence. To support the mechanism, examine its predicted behavior relative to the current design and cases where it could regress, with comparable resources. Continue unresolved questions across sessions instead of inventing a new one on every turn. A worker's report is evidence to assess, not a certified conclusion.
