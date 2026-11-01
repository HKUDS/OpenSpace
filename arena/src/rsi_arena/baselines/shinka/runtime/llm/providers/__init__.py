"""Provider result type; concrete SDK-backed providers load on first call.

Adapted by RSI Arena: eager provider imports are avoided so the vendored extra
only requires the SDK selected by an actual model route.
"""

from .result import QueryResult

__all__ = ["QueryResult"]
