# Terminal-Bench 2.1 extra

Developer reference for the TB2.1 adapter. Start with [Arena setup](../../../../README.md) and its [verifier preparation](../../../../docs/tb21-offline-verifier.md), or use the [AgentPilot integration guide](../../../../../agentpilot/integrations/arena/README.md).

This package owns the Terminal-Bench 2.1 adapter, frozen official task identity, candidate/result binding, development publication, exact verifier authority, and Harbor-backed execution mapping. It consumes the shared `rsi_arena.benchmarks.harbor_runtime` substrate and does not define a method-specific A/B policy.

The default Arena kernel does not import this package. A release composition owner may explicitly construct the adapter from this package when TB2.1 is a supported benchmark.

The product composition supplies one cell-wide inner-agent selection. TB2.1 owns its model route, task images, timeout authority, and offline verifier closure. Method launch policy remains outside the benchmark package.
