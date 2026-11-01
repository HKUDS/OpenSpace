"""Select the worker implementation without changing orchestration."""

from dataclasses import asdict

from .worker_settings import WorkerRuntimeSettings, worker_settings


def create_worker_harness(
    runtime, *, environment, purpose="build", refusal_ledger=None
):
    # Reuse existing task prompts; the external CLI owns its own agent loop.
    from .action_connectors import BoundedCodexCliHarness, CodexCliConfig
    from .research_experiment import ExperimentCodexCliHarness
    from agentpilot_arena.research_reuse import FullInstructionHarness

    prompt_classes = {
        "build": FullInstructionHarness,
        "experiment": ExperimentCodexCliHarness,
        "action": BoundedCodexCliHarness,
    }
    if purpose not in prompt_classes:
        raise ValueError(f"unknown worker purpose {purpose!r}")
    prompt_class = prompt_classes[purpose]
    settings = worker_settings(runtime)
    if isinstance(settings, WorkerRuntimeSettings):
        from .worker_cli import ExternalCliHarness

        if purpose == "action":
            raise ValueError("external CLI workers require the build/experiment research workflow")
        return ExternalCliHarness(
            settings, environment=environment, prompt_builder=prompt_class._prompt
        )
    return prompt_class(
        CodexCliConfig(**asdict(settings)),
        environment=environment,
        refusal_ledger=refusal_ledger,
    )


def worker_accounting(result):
    """Transport explicit external billing through saved research receipts."""
    accounting = result.diagnostics.get("accounting")
    if (
        result.cost_usd is None
        and isinstance(accounting, dict)
        and accounting.get("mode") == "external"
    ):
        return {"worker_accounting": dict(accounting)}
    return {}
