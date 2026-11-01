---
name: rsi-arena
description: Set up or run RSI Arena benchmark evaluations from HKUDS AgentPilot. Use for AIME, Terminal-Bench, and comparing improvement methods; use agentpilot for the unified RSI entry or ordinary coding work.
---

# RSI Arena

This skill guides the existing benchmark workflow; it does not install benchmark dependencies or run paid evaluations merely by being added to a host.

Reuse an existing AgentPilot checkout, or obtain https://github.com/HKUDS/AgentPilot in a separate directory. Read `arena/README.md` and the chosen benchmark's setup instructions. For AgentPilot policies, follow `agentpilot/integrations/arena/README.md`.

Infer the requested benchmark and method from the user's goal; ask only if these choices are missing. Install that component's environment and verify its task data, model configuration, execution dependencies, and evaluator before launch. Use the selected formal benchmark runner and its evaluation protocol. Run within the user's requested scope and budget, preserve test boundaries, and report the actual run directory, observed metrics, and any missing prerequisites.
