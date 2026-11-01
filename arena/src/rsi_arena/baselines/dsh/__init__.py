"""DeepSeek Harness SDK baseline extra."""

from rsi_arena.baselines.dsh.launcher import resume_dsh, run_dsh, run_new_dsh
from rsi_arena.baselines.dsh.native import (
    DSH_API_BASE_URL_ENV,
    DSH_CORDIS_PATH,
    DSH_CREDENTIAL_ENV,
    DSH_MODEL_ENV,
    DSH_OFFICIAL_PROVIDER,
    DSH_RUNTIME_LINUX_X86_64_WHEEL_SHA256,
    DSH_RUNTIME_VERSION,
    DSH_SDK_VERSION,
    DSH_SDK_WHEEL_SHA256,
    DshSdkSession,
    DshSdkSessionAuthority,
    DshSdkSessionConfig,
    official_dsh_identity,
    project_dsh_sdk_environment,
    resolve_dsh_sdk_session_authority,
)

__all__ = [
    "DSH_API_BASE_URL_ENV",
    "DSH_CORDIS_PATH",
    "DSH_CREDENTIAL_ENV",
    "DSH_MODEL_ENV",
    "DSH_OFFICIAL_PROVIDER",
    "DSH_RUNTIME_LINUX_X86_64_WHEEL_SHA256",
    "DSH_RUNTIME_VERSION",
    "DSH_SDK_VERSION",
    "DSH_SDK_WHEEL_SHA256",
    "DshSdkSession",
    "DshSdkSessionAuthority",
    "DshSdkSessionConfig",
    "official_dsh_identity",
    "project_dsh_sdk_environment",
    "resolve_dsh_sdk_session_authority",
    "resume_dsh",
    "run_dsh",
    "run_new_dsh",
]
