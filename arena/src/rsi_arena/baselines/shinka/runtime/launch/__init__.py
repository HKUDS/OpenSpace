"""Arena-only evaluator seam for the vendored Shinka derivative.

Adapted by RSI Arena: external evaluator processes and Slurm launch modes are
not part of the supported Arena derivative.
"""

from .scheduler import (
    ArenaCandidateEvaluator,
    ArenaJobConfig,
    ArenaJobHandle,
    JobConfig,
    JobScheduler,
)

__all__ = [
    "ArenaCandidateEvaluator",
    "ArenaJobConfig",
    "ArenaJobHandle",
    "JobConfig",
    "JobScheduler",
]
