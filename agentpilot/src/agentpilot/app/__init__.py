"""Lazy public facade for application assembly and the local batch loop."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ActionRunnerBinding": (".bindings", "ActionRunnerBinding"),
    "assert_action_execution_fence_live": (
        ".bindings",
        "assert_action_execution_fence_live",
    ),
    "AssemblyError": (".bindings", "AssemblyError"),
    "BatchLoop": (".batch_loop", "BatchLoop"),
    "BatchResult": (".contracts", "BatchResult"),
    "CampaignAssessment": (".campaign", "CampaignAssessment"),
    "CampaignDisposition": (".campaign", "CampaignDisposition"),
    "ReconciliationWindow": (".campaign", "ReconciliationWindow"),
    "CampaignAudit": (".campaign_audit", "CampaignAudit"),
    "audit_campaign": (".campaign_audit", "audit_campaign"),
    "EmbeddingAccountingAudit": (
        ".embedding_audit",
        "EmbeddingAccountingAudit",
    ),
    "audit_embedding_accounting": (
        ".embedding_audit",
        "audit_embedding_accounting",
    ),
    "CampaignConfig": (".runtime_config", "CampaignConfig"),
    "CampaignOutcome": (".campaign_driver", "CampaignOutcome"),
    "CampaignStatus": (".campaign_driver", "CampaignStatus"),
    "CampaignSupervisor": (".campaign_driver", "CampaignSupervisor"),
    "SubprocessBatchInvoker": (".campaign_driver", "SubprocessBatchInvoker"),
    "assess_batch": (".campaign", "assess_batch"),
    "BatchAssemblyContext": (".bindings", "BatchAssemblyContext"),
    "CarrierUpdateBatch": (".contracts", "CarrierUpdateBatch"),
    "CarrierUpdateRecoveryReader": (
        ".recovery",
        "CarrierUpdateRecoveryReader",
    ),
    "CanonicalObservationBuilder": (
        ".observations",
        "CanonicalObservationBuilder",
    ),
    "CanonicalArchiveTransitioner": (
        ".archive",
        "CanonicalArchiveTransitioner",
    ),
    "CanonicalEvidenceProvider": (".inputs", "CanonicalEvidenceProvider"),
    "CanonicalDirectionHistoryProvider": (
        ".inputs",
        "CanonicalDirectionHistoryProvider",
    ),
    "CanonicalParentSlateProvider": (
        ".inputs",
        "CanonicalParentSlateProvider",
    ),
    "CanonicalSearchInputs": (".inputs", "CanonicalSearchInputs"),
    "CanonicalSearchStateBuilder": (
        ".inputs",
        "CanonicalSearchStateBuilder",
    ),
    "CanonicalTraceEvidenceProvider": (
        ".inputs",
        "CanonicalTraceEvidenceProvider",
    ),
    "CanonicalSearchReadModelWriter": (
        ".search_read_model",
        "CanonicalSearchReadModelWriter",
    ),
    "CanonicalUCBRoundInputProvider": (
        ".inputs",
        "CanonicalUCBRoundInputProvider",
    ),
    "CompletedWork": (".executor", "CompletedWork"),
    "CooldownConfig": (".config", "CooldownConfig"),
    "ExecutionResult": (".contracts", "ExecutionResult"),
    "EvaluationPlanFreezer": (".contracts", "EvaluationPlanFreezer"),
    "EnvironmentConfig": (".runtime_config", "EnvironmentConfig"),
    "DurableCompletion": (".provider_calls", "DurableCompletion"),
    "DurablePromptCompletion": (
        ".model_harnesses",
        "DurablePromptCompletion",
    ),
    "DurableSearchInputSource": (
        ".search_read_model",
        "DurableSearchInputSource",
    ),
    "TRACE_EVIDENCE_UNAVAILABLE": (
        ".search_read_model",
        "TRACE_EVIDENCE_UNAVAILABLE",
    ),
    "TraceEvidenceUnavailable": (
        ".search_read_model",
        "TraceEvidenceUnavailable",
    ),
    "LocalExecutor": (".executor", "LocalExecutor"),
    "PlannedAction": (".contracts", "PlannedAction"),
    "PlanningFailureRecoveryReader": (
        ".recovery",
        "PlanningFailureRecoveryReader",
    ),
    "PolicyConfig": (".config", "PolicyConfig"),
    "PolicyConfigError": (".config", "PolicyConfigError"),
    "ProviderCallAttemptAlreadyFailed": (
        ".provider_calls",
        "ProviderCallAttemptAlreadyFailed",
    ),
    "ProviderCallContext": (".provider_calls", "ProviderCallContext"),
    "ProviderProposerHarness": (
        ".model_harnesses",
        "ProviderProposerHarness",
    ),
    "ProviderRegistry": (".bindings", "ProviderRegistry"),
    "ProviderDirectionEmbeddingBackend": (
        ".semantic_encoder",
        "ProviderDirectionEmbeddingBackend",
    ),
    "RecoverableUCBRuntime": (".ucb_runtime", "RecoverableUCBRuntime"),
    "RuntimeConfig": (".runtime_config", "RuntimeConfig"),
    "RuntimeConfigError": (".runtime_config", "RuntimeConfigError"),
    "SearchInputSnapshot": (".inputs", "SearchInputSnapshot"),
    "SearchReadModelMaterializer": (
        ".contracts",
        "SearchReadModelMaterializer",
    ),
    "SealedRunFinalizer": (".sealed_lifecycle", "SealedRunFinalizer"),
    "StructuredProposalParseError": (
        ".model_harnesses",
        "StructuredProposalParseError",
    ),
    "TerminalBatchError": (".contracts", "TerminalBatchError"),
    "NamedApplicabilityBuilder": (".bindings", "NamedApplicabilityBuilder"),
    "NamedNearStatePredicate": (".bindings", "NamedNearStatePredicate"),
    "AssemblyDependencies": (".bindings", "AssemblyDependencies"),
    "BatchRuntime": (".lease_runtime", "BatchRuntime"),
    "AppConfig": (".runtime_config", "AppConfig"),
    "Environment": (".bindings", "Environment"),
    "RuntimeFactory": (".assembly", "RuntimeFactory"),
    "load_policy_config": (".config", "load_policy_config"),
    "load_app_config": (".runtime_config", "load_app_config"),
    "parse_runtime_config": (".runtime_config", "parse_runtime_config"),
    "parse_app_config": (".runtime_config", "parse_app_config"),
    "parse_policy_config": (".config", "parse_policy_config"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
