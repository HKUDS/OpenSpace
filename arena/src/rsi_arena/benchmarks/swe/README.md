# SWE-bench Verified extra

Developer reference for the SWE adapter. For installation and usage, start with [Arena setup](../../../../README.md), then follow [SWE preparation](../../../../docs/swe-bench-verified.md). AgentPilot has a separate [integration guide](../../../../../agentpilot/integrations/arena/README.md).

This extra owns the concrete SWE-bench Verified task and execution code. It is not imported by the benchmark-agnostic Arena lifecycle; the kernel composition root constructs it only when `benchmark.name` selects SWE.

The runtime path is deliberately narrow:

1. `tasks.py` derives immutable Harbor task packages and keeps held-out material evaluator-only.
2. `campaign.py` binds Arena-owned attempt slots and maps trusted cell results to `measured` or `inconclusive` outcomes.
3. Shared `harbor_runtime/frozen_mini_agent.py` owns the native mini-swe-agent loop; `harbor_backend.py` supplies SWE task and tree-artifact authority.
4. `runtime.py` freezes the stopped candidate tree and derives the patch.
5. `docker_runtime.py` invokes the content-pinned SWE verifier.

The product supplies one cell-wide inner agent. `builtin:mini-swe-agent` is available only for a SWE-only cell and accepts no config fields; `builtin:terminus-2` and explicit Python agents use the shared bridge. SWE retains sole authority over the task working directory, candidate-tree export, patch collection, and separate verifier boundary.

Tests under `arena/tests/test_swe_*` use fake/no-model subprocesses. Running a real model, Docker, or Harbor route remains an explicit operator action.
