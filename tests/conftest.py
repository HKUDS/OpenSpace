from __future__ import annotations

import sys
from types import ModuleType
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp_stub = ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *, total=None):
            self.total = total

    class _ClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def close(self):
            return None

    class _TCPConnector:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ClientResponse:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _ClientResponseError(Exception):
        def __init__(self, *args, status=None, message="", **kwargs):
            super().__init__(message)
            self.status = status
            self.message = message

    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = _ClientSession
    aiohttp_stub.TCPConnector = _TCPConnector
    aiohttp_stub.ClientResponse = _ClientResponse
    aiohttp_stub.ClientResponseError = _ClientResponseError
    sys.modules["aiohttp"] = aiohttp_stub

try:
    import yarl  # noqa: F401
except ModuleNotFoundError:
    yarl_stub = ModuleType("yarl")

    class _URL(str):
        def __new__(cls, value="", *args, **kwargs):
            return str.__new__(cls, value)

        def with_path(self, value):
            return type(self)(value)

        def join(self, other):
            return type(self)(f"{self.rstrip('/')}/{str(other).lstrip('/')}")

    yarl_stub.URL = _URL
    sys.modules["yarl"] = yarl_stub
