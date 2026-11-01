"""Public RSI Arena SDK and lazy trusted-runtime exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .client import (
    ARENA_CLIENT_STATE_ENV,
    ARENA_SOCKET_ENV,
    ArenaClient,
    ArenaClientError,
    ArenaClientStateError,
    ArenaClientTransportError,
    ArenaRequestError,
    ArenaResult,
)
from .dto import (
    ArenaBenchmarkScore,
    ArenaError,
    ArenaProtocolError,
    ArenaRequest,
    ArenaResponse,
    ArenaSubmitResult,
    DevelopmentBatchResult,
)
from .transport import ArenaTransportError, ArenaUnixServer, ArenaUnixTransport

if TYPE_CHECKING:
    from .development import (
        DevelopmentAdapter,
        DevelopmentArtifact,
        DevelopmentExecution,
        DevelopmentMaterial,
        DevelopmentOperations,
        RunScopedDevelopmentOperations,
    )
    from .experiment import (
        ExperimentAlreadyExistsError,
        ExperimentDocumentError,
        canonical_experiment_bytes,
        experiment_digest,
        load_experiment_toml,
        read_experiment,
        resolve_experiment,
        write_experiment,
    )
    from .held_out_evaluation import (
        HeldOutEvaluationError,
        run_held_out_evaluation,
    )
    from .integration import (
        BenchmarkIntegration,
        IntegrationFactory,
        TrustedResolvedCampaignConfig,
    )
    from .launcher import CommandLaunchError, launch_command, resume_command
    from .manifests import SampleSlot
    from .native import open_native_session, resume_native_session
    from .projection import (
        ArenaProjectionError,
        ArenaRunAuthority,
        ArenaRunProjection,
        open_run_replay,
    )
    from .runner import ArenaSession, MethodRunner
    from .service import (
        ArenaCandidateRejected,
        ArenaService,
        CampaignSubmissionOperations,
    )

_LAZY_EXPORTS = {
    "ArenaService": ".service",
    "ArenaCandidateRejected": ".service",
    "CampaignSubmissionOperations": ".service",
    "ArenaSession": ".runner",
    "MethodRunner": ".runner",
    "ArenaProjectionError": ".projection",
    "ArenaRunAuthority": ".projection",
    "ArenaRunProjection": ".projection",
    "open_run_replay": ".projection",
    "ExperimentAlreadyExistsError": ".experiment",
    "ExperimentDocumentError": ".experiment",
    "canonical_experiment_bytes": ".experiment",
    "experiment_digest": ".experiment",
    "load_experiment_toml": ".experiment",
    "read_experiment": ".experiment",
    "resolve_experiment": ".experiment",
    "write_experiment": ".experiment",
    "CommandLaunchError": ".launcher",
    "launch_command": ".launcher",
    "resume_command": ".launcher",
    "open_native_session": ".native",
    "resume_native_session": ".native",
    "HeldOutEvaluationError": ".held_out_evaluation",
    "run_held_out_evaluation": ".held_out_evaluation",
    "SampleSlot": ".manifests",
    "BenchmarkIntegration": ".integration",
    "IntegrationFactory": ".integration",
    "TrustedResolvedCampaignConfig": ".integration",
    "DevelopmentAdapter": ".development",
    "DevelopmentArtifact": ".development",
    "DevelopmentExecution": ".development",
    "DevelopmentMaterial": ".development",
    "DevelopmentOperations": ".development",
    "RunScopedDevelopmentOperations": ".development",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "ARENA_CLIENT_STATE_ENV",
    "ARENA_SOCKET_ENV",
    "ArenaBenchmarkScore",
    "ArenaCandidateRejected",
    "ArenaClient",
    "ArenaClientError",
    "ArenaClientStateError",
    "ArenaClientTransportError",
    "ArenaError",
    "ArenaProjectionError",
    "ArenaProtocolError",
    "ArenaRequest",
    "ArenaRequestError",
    "ArenaResponse",
    "ArenaResult",
    "ArenaRunAuthority",
    "ArenaRunProjection",
    "ArenaService",
    "ArenaSession",
    "ArenaSubmitResult",
    "ArenaTransportError",
    "ArenaUnixServer",
    "ArenaUnixTransport",
    "BenchmarkIntegration",
    "CampaignSubmissionOperations",
    "CommandLaunchError",
    "DevelopmentAdapter",
    "DevelopmentArtifact",
    "DevelopmentBatchResult",
    "DevelopmentExecution",
    "DevelopmentMaterial",
    "DevelopmentOperations",
    "ExperimentAlreadyExistsError",
    "ExperimentDocumentError",
    "HeldOutEvaluationError",
    "IntegrationFactory",
    "MethodRunner",
    "RunScopedDevelopmentOperations",
    "SampleSlot",
    "TrustedResolvedCampaignConfig",
    "canonical_experiment_bytes",
    "experiment_digest",
    "launch_command",
    "load_experiment_toml",
    "open_native_session",
    "open_run_replay",
    "read_experiment",
    "resolve_experiment",
    "resume_command",
    "resume_native_session",
    "run_held_out_evaluation",
    "write_experiment",
]
