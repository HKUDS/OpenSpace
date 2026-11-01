---
name: proposer-guide
description: Use source and execution tools to investigate a supplied program and check a proposed change against evidence.
---

# Read source and execution evidence

Read missing code before relying on a name, diagram or docstring. Follow related_implementations into callees when a claim depends on their behavior.

Use paths and record references from the supplied directories:
- list_sources() browses code; read(path="src/main.py", line=40) reads file lines.
- search(query="text") searches source and opens a unique match when possible; use scope="trace" to search executions.
- list_cases(view="tasks") reads task requirements independently of outcomes. list_cases() browses executions; read_trace(ref="<record reference>") opens one.
- check_trace(ref, start, end, name) optionally counts and orders recorded tool requests in an event interval; omit name to include all tools. It checks facts, not whether the proposed change would improve results.
- Follow returned next calls to continue, open calls to inspect a match, and expand calls to recover omitted text. No cursor conversion is needed.

Inspect relevant attempts before connecting their observations. Successful repeats help compare behavior, but neither success nor failure proves a cause. Executor explanations and printed self-verdicts are claims. A recorded tool result may mix terminal output and harness feedback; keep an unknown origin unknown. Keep early ideas provisional: check whether the change already exists, would trigger, or would still accept the recorded failure. Check whether its new action would occur in time to affect that failure. When counterevidence rules out the idea, revise the mechanism or narrow its applicability.

Independent reads may share a response; wait for results when the next action depends on them.
