from .sandbox import BaseSandbox, SandboxManager
from .policies import SecurityPolicyManager, SecurityPolicy

# Try to import E2BSandbox (optional dependency)
try:
    from .e2b_sandbox import E2BSandbox
    E2B_AVAILABLE = True
except ImportError:
    E2BSandbox = None
    E2B_AVAILABLE = False

__all__ = [
    "BaseSandbox",
    "SandboxManager",
    "SecurityPolicyManager",
    "SecurityPolicy"
]

if E2B_AVAILABLE:
    __all__.append("E2BSandbox")