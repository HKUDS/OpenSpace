# BrowseComp-Plus benchmark

Developer reference for the BrowseComp adapter. For installation and usage, start with [Arena setup](../../../../README.md), then follow [BrowseComp preparation](../../../../docs/browsecomp-plus.md). AgentPilot has a separate [integration guide](../../../../../agentpilot/integrations/arena/README.md).

This package owns the 830-task BrowseComp-Plus catalog contract, deterministic 249-task development boundary, evaluator-only 581-task held-out boundary, BM25 search service, answer/citation parser, judge, and Harbor Terminus-2 adapter.

Production construction is fail-closed. Before a run is frozen it validates the complete catalog and corpus, opens the configured Lucene index through Pyserini, checks Java and tiktoken, resolves judge and agent-runner source, and records content digests for every resource. Resume repeats those checks and rejects resource drift. Construction never calls a model provider.

The candidate receives only one query and a task-local Unix search endpoint. Search returns at most five results with 512-token snippets. Answers, qrels, held-out IDs, the raw judge response, corpus paths, and index paths remain in the trusted benchmark owner. Development can optionally publish retrieved snippets or raw judge text; both are explicit policy choices.

The cell-wide inner agent may be `builtin:terminus-2` or a `python:<module>:<callable>` selection. Benchmark configuration separately binds the solver model, trusted runner, judge model, provider profiles, total task deadline, and retrieval concurrency. Provider-reported solver and judge tokens/cost are retained in task provenance and projected into Arena usage accounting when available.

The packaged prompt scaffold uses the reference runner's `python:browsecomp_agent:run` selection. The agent searches as needed, then makes one sole `submit_answer` tool call. The trusted runner validates its three arguments and renders the evaluator's canonical labeled response; malformed, mixed, duplicate, or free-text termination has no fallback. The selection is an opaque identity for the trusted reference runner; the prompt-only candidate does not need a Python module.
