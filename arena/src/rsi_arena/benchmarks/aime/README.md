# AIME benchmark extra

Developer reference for the AIME adapter. For installation and usage, follow [Arena setup](../../../../README.md) or the [AgentPilot integration guide](../../../../../agentpilot/integrations/arena/README.md).

This extra provides the repository-supported AIME 2024+2025 benchmark adapter. It is concrete benchmark code and is not part of the benchmark-agnostic Arena kernel.

The committed `data/aime_2024_2025.json` snapshot contains 60 problems from the four 2024/2025 AIME contests. `tasks.py` deterministically renders selected problems into Harbor task directories. The rendered agent environment contains the public problem only; answers remain in the separate trusted verifier material.

Candidates write `/app/answer.txt`. The trusted Harbor bridge publishes a bounded integer artifact at `/logs/artifacts/rsi-arena-answer.txt`, and the fresh verifier performs strict exact-match scoring without executing candidate bytes. The snapshot, rendered tasks, material, runtime, and verifier identities are content-bound in the benchmark identity.

`adapter.py` owns trusted task execution. `development.py` exposes only the frozen development material and public development result/trajectory. `campaign.py` evaluates Arena-owned attempt slots, while `integration.py` connects both surfaces to the method-neutral composition boundary. A method may partition the development set into its own A/B policy; the benchmark does not.
