"""Persistence ports and reference in-memory implementation."""

from agentpilot.persistence.leases import (
    RunBatchLease,
    RunBatchLeaseManager,
    RunLeaseLost,
    RunLeaseUnavailable,
)
from agentpilot.persistence.memory import (
    InMemoryDatabase,
    InMemoryUnitOfWork,
    InMemoryUnitOfWorkFactory,
    PersistenceConflict,
)
from agentpilot.persistence.protocols import (
    ActionEvaluationPlanRepository,
    ArchiveTransition,
    BatchRecord,
    BatchStatus,
    EvaluationPriorRepository,
    LineageEdge,
    PlanningFailureRepository,
    SearchReadModelRepository,
    UnitOfWork,
    UnitOfWorkFactory,
)
from agentpilot.persistence.sealed import (
    SQLiteSealedRepository,
    TransactionalSealedRepository,
)
from agentpilot.persistence.sqlite import (
    PERSISTENCE_CONTRACT,
    PersistenceContractMismatch,
    SQLiteFencedUnitOfWorkFactory,
    SQLiteReadOnlyUnitOfWork,
    SQLiteReadOnlyUnitOfWorkFactory,
    SQLiteRunBatchLeaseManager,
    SQLiteUnitOfWork,
    SQLiteUnitOfWorkFactory,
)

__all__ = [
    "ActionEvaluationPlanRepository",
    "ArchiveTransition",
    "BatchRecord",
    "BatchStatus",
    "EvaluationPriorRepository",
    "InMemoryDatabase",
    "InMemoryUnitOfWork",
    "InMemoryUnitOfWorkFactory",
    "LineageEdge",
    "PlanningFailureRepository",
    "PERSISTENCE_CONTRACT",
    "PersistenceContractMismatch",
    "SearchReadModelRepository",
    "PersistenceConflict",
    "RunBatchLease",
    "RunBatchLeaseManager",
    "RunLeaseLost",
    "RunLeaseUnavailable",
    "SQLiteFencedUnitOfWorkFactory",
    "SQLiteReadOnlyUnitOfWork",
    "SQLiteReadOnlyUnitOfWorkFactory",
    "SQLiteRunBatchLeaseManager",
    "SQLiteSealedRepository",
    "SQLiteUnitOfWork",
    "SQLiteUnitOfWorkFactory",
    "TransactionalSealedRepository",
    "UnitOfWork",
    "UnitOfWorkFactory",
]
