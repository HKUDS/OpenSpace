"""Cloud platform integration.

Provides:
  - ``OpenSpaceClient`` — HTTP client for the cloud API
  - ``get_openspace_auth`` — credential resolution
  - ``SkillSearchEngine`` — hybrid BM25 + embedding search
  - ``generate_embedding`` — OpenAI embedding generation
"""

from openspace.cloud.auth import get_openspace_auth


def __getattr__(name: str):
    if name == "OpenSpaceClient":
        from openspace.cloud.client import OpenSpaceClient
        return OpenSpaceClient
    if name == "SkillSearchEngine":
        from openspace.cloud.search import SkillSearchEngine
        return SkillSearchEngine
    if name == "generate_embedding":
        from openspace.cloud.embedding import generate_embedding
        return generate_embedding
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "OpenSpaceClient",
    "get_openspace_auth",
    "SkillSearchEngine",
    "generate_embedding",
]
