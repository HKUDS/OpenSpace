from __future__ import annotations

from openspace.grounding.core.tool.base import BaseTool
import re
import os
import httpx
from typing import Iterable, List, Tuple, Dict, Optional, Any, TYPE_CHECKING
from enum import Enum
import json
import pickle
from pathlib import Path
from datetime import datetime

from .tool import BaseTool
from .types import BackendType
from .tool_discovery import rank_tools_by_keyword
from openspace.utils.logging import Logger
from openspace.config.constants import PROJECT_ROOT

if TYPE_CHECKING:
    from .quality import ToolQualityManager
    from openspace.llm import LLMClient

logger = Logger.get_logger(__name__)
_DEFAULT_LLM = object()
_NUMPY: Any = None


def _np() -> Any:
    global _NUMPY
    if _NUMPY is None:
        import numpy as np

        _NUMPY = np
    return _NUMPY


class SearchMode(str, Enum):
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class ToolRanker:
    """
    ToolRanker: rank tools by keyword, semantic or hybrid
    """
    # Cache version for persistent storage - increment when cache format changes
    CACHE_VERSION = 1
    
    def __init__(
        self, 
        model_name: Optional[str] = None,
        cache_dir: Optional[str | Path] = None,
        enable_cache_persistence: bool = False
    ):
        """Initialize ToolRanker.
        
        Args:
            model_name: Embedding model name. If None, will use env or config value.
            cache_dir: Directory to store persistent embedding cache.
            enable_cache_persistence: Whether to persist embeddings to disk.
        """
        # Check for remote API config from environment
        self._api_base_url = os.getenv("EMBEDDING_BASE_URL")
        self._api_key = os.getenv("EMBEDDING_API_KEY")
        self._use_remote_api = bool(self._api_key and self._api_base_url)
        
        # Get model name: env > param > config > default
        if model_name is None:
            model_name = os.getenv("EMBEDDING_MODEL")
        
        if model_name is None:
            try:
                from openspace.config import get_config
                config = get_config()
                model_name = config.tool_search.embedding_model
            except Exception as exc:
                logger.warning(f"Failed to load config, using default model: {exc}")
                model_name = "BAAI/bge-small-en-v1.5"
        
        self._model_name = model_name
        self._embed_model = None  # lazy load
        self._embedding_fn = None
        
        if self._use_remote_api:
            logger.info(f"Using remote embedding API: {self._api_base_url}, model: {model_name}")
        
        # Persistent cache settings
        self._enable_cache_persistence = enable_cache_persistence
        if cache_dir is None:
            cache_dir = PROJECT_ROOT / ".openspace" / "embedding_cache"
        self._cache_dir = Path(cache_dir)
        
        # Log cache settings
        logger.info(
            f"ToolRanker initialized: enable_cache_persistence={enable_cache_persistence}, "
            f"cache_dir={self._cache_dir}"
        )
        
        # Structured in-memory cache
        # Structure: {backend: {server: {tool_name: {"embedding": np.ndarray, "description": str, "cached_at": str}}}}
        self._structured_cache: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        
        # For backward compatibility and quick lookup: {text -> (backend, server, tool_name)}
        self._text_to_key: Dict[str, Tuple[str, str, str]] = {}
        
        # Load persistent cache if enabled
        if self._enable_cache_persistence:
            logger.info(f"Loading persistent cache from {self._cache_dir}")
            self._load_persistent_cache()
    
    def _get_cache_key(self, tool: BaseTool) -> Tuple[str, str, str]:
        """Get structured cache key (backend, server, tool_name) from tool."""
        if tool.is_bound:
            backend = tool.runtime_info.backend.value
            server = tool.runtime_info.server_name or "default"
        else:
            if not tool.backend_type or tool.backend_type == BackendType.NOT_SET:
                backend = "UNKNOWN"
            else:
                backend = tool.backend_type.value
            server = "default"
        
        return (backend, server, tool.name)
    
    def _get_cache_file_path(self) -> Path:
        """Get the cache file path for the current model."""
        # Use model name in filename to support multiple models
        safe_model_name = self._model_name.replace("/", "_").replace("\\", "_")
        return self._cache_dir / f"embeddings_{safe_model_name}_v{self.CACHE_VERSION}.pkl"
    
    def _load_persistent_cache(self) -> None:
        """Load embeddings from disk cache."""
        cache_file = self._get_cache_file_path()
        
        if not cache_file.exists():
            logger.debug(f"No persistent cache found at {cache_file}")
            return
        
        try:
            with open(cache_file, 'rb') as f:
                data = pickle.load(f)
            
            # Validate cache version
            if isinstance(data, dict) and data.get("version") == self.CACHE_VERSION:
                self._structured_cache = data.get("embeddings", {})
                self._rebuild_text_index()
                
                # Count total embeddings
                total = sum(
                    len(tools) 
                    for backend in self._structured_cache.values() 
                    for tools in backend.values()
                )
                logger.info(f"Loaded {total} embeddings from cache: {cache_file}")
            else:
                logger.warning(f"Cache version mismatch or invalid format, starting fresh")
                self._structured_cache = {}
        except Exception as exc:
            logger.warning(f"Failed to load persistent cache: {exc}")
            self._structured_cache = {}
    
    def _rebuild_text_index(self) -> None:
        """Rebuild text-to-key mapping for quick lookup."""
        self._text_to_key.clear()
        for backend, servers in self._structured_cache.items():
            for server, tools in servers.items():
                for tool_name, tool_data in tools.items():
                    desc = tool_data.get("description", "")
                    text = f"{tool_name}: {desc}"
                    self._text_to_key[text] = (backend, server, tool_name)
    
    def _save_persistent_cache(self) -> None:
        """Save embeddings to disk cache."""
        if not self._enable_cache_persistence or not self._structured_cache:
            return
        
        cache_file = self._get_cache_file_path()
        
        try:
            # Create directory if it doesn't exist
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Build cache data with metadata
            cache_data = {
                "version": self.CACHE_VERSION,
                "model_name": self._model_name,
                "last_updated": datetime.now().isoformat(),
                "embeddings": self._structured_cache
            }
            
            # Save cache
            with open(cache_file, 'wb') as f:
                pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
            
            # Count total embeddings
            total = sum(
                len(tools) 
                for backend in self._structured_cache.values() 
                for tools in backend.values()
            )
            logger.debug(f"Saved {total} embeddings to cache: {cache_file}")
        except Exception as exc:
            logger.warning(f"Failed to save persistent cache: {exc}")

    def rank(
        self,
        query: str,
        tools: List[BaseTool],
        *,
        top_k: int = 50,
        mode: SearchMode = SearchMode.SEMANTIC,
    ) -> List[Tuple[BaseTool, float]]:
        if mode == SearchMode.KEYWORD:
            return self._keyword_search(query, tools, top_k)
        if mode == SearchMode.SEMANTIC:
            return self._semantic_search(query, tools, top_k)
        # hybrid
        return self._hybrid_search(query, tools, top_k)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens = re.split(r"[^\w]+", text.lower())
        tokens = [tok for tok in tokens if tok]
        return tokens

    def _keyword_search(
        self, query: str, tools: Iterable[BaseTool], top_k: int
    ) -> List[Tuple[BaseTool, float]]:
        try:
            from rank_bm25 import BM25Okapi  # type: ignore
        except ImportError:
            BM25Okapi = None  # fallback below

        tool_list = list(tools)
        if not tool_list:
            return []
        
        corpus_tokens: list[list[str]] = [self._tokenize(f"{t.name} {t.description}") for t in tool_list]
        query_tokens = self._tokenize(query)

        if BM25Okapi and corpus_tokens:
            bm25 = BM25Okapi(corpus_tokens)
            scores = bm25.get_scores(query_tokens)
            scored = [(t, float(s)) for t, s in zip(tool_list, scores, strict=True)]
        else:
            # fallback: simple term overlap ratio
            q_set = set(query_tokens)
            scored = []
            for t, toks in zip(tool_list, corpus_tokens, strict=True):
                if not toks:
                    scored.append((t, 0.0))  # Include tool with 0 score
                    continue
                overlap = q_set.intersection(toks)
                score = len(overlap) / len(q_set) if len(q_set) > 0 else 0.0
                scored.append((t, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        result = scored[:top_k]
        
        # If no matches found (all scores are 0), return all tools
        if not result or all(score == 0.0 for _, score in result):
            logger.debug(f"Keyword search found no matches, returning all {len(tool_list)} tools")
            return [(t, 0.0) for t in tool_list]
        
        return result

    def _ensure_model(self) -> bool:
        """Ensure embedding model is ready (local or remote)."""
        if self._embedding_fn is not None:
            return True
        
        if self._use_remote_api:
            return self._init_remote_embedding()
        return self._init_local_embedding()

    def _init_remote_embedding(self) -> bool:
        """Initialize remote embedding API (OpenRouter/OpenAI compatible)."""
        try:
            def embed_texts(texts: List[str]) -> List[np.ndarray]:
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(
                        f"{self._api_base_url}/embeddings",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json"
                        },
                        json={"model": self._model_name, "input": texts}
                    )
                    response.raise_for_status()
                    data = response.json()
                    return [_np().array(item["embedding"]) for item in data["data"]]
            
            self._embedding_fn = embed_texts
            logger.info(f"Remote embedding API initialized: {self._model_name}")
            return True
        except Exception as exc:
            logger.error(f"Failed to initialize remote embedding API: {exc}")
            return False

    def _init_local_embedding(self) -> bool:
        """Initialize local fastembed model."""
        try:
            from fastembed import TextEmbedding 
            logger.debug(f"fastembed imported successfully, loading model: {self._model_name}")
        except ImportError as e:
            logger.warning(
                f"fastembed not installed (ImportError: {e}), semantic search unavailable. "
                f"Install with: pip install fastembed"
            )
            return False
        
        try:
            logger.info(f"Loading embedding model: {self._model_name}...")
            self._embed_model = TextEmbedding(model_name=self._model_name)
            self._embedding_fn = lambda txts: list(self._embed_model.embed(txts))
            logger.info(f"Embedding model '{self._model_name}' loaded successfully")
            return True
        except Exception as exc:
            logger.error(f"Embedding model '{self._model_name}' loading failed: {exc}")
            return False

    def _get_embedding(self, tool: BaseTool) -> Optional[np.ndarray]:
        """Get embedding from structured cache."""
        backend, server, tool_name = self._get_cache_key(tool)
        
        if backend not in self._structured_cache:
            return None
        if server not in self._structured_cache[backend]:
            return None
        if tool_name not in self._structured_cache[backend][server]:
            return None
        
        return self._structured_cache[backend][server][tool_name].get("embedding")
    
    def _set_embedding(self, tool: BaseTool, embedding: np.ndarray) -> None:
        """Store embedding in structured cache."""
        backend, server, tool_name = self._get_cache_key(tool)
        
        # Initialize nested structure if needed
        if backend not in self._structured_cache:
            self._structured_cache[backend] = {}
        if server not in self._structured_cache[backend]:
            self._structured_cache[backend][server] = {}
        
        # Store embedding with metadata
        self._structured_cache[backend][server][tool_name] = {
            "embedding": embedding,
            "description": tool.description or "",
            "cached_at": datetime.now().isoformat()
        }
        
        # Update text index for backward compatibility
        text = f"{tool.name}: {tool.description}"
        self._text_to_key[text] = (backend, server, tool_name)
    
    def _semantic_search(
        self, query: str, tools: Iterable[BaseTool], top_k: int
    ) -> List[Tuple[BaseTool, float]]:
        if not self._ensure_model():
            logger.debug("Semantic search unavailable, returning empty list")
            return []
        
        tools_list = list(tools)
        
        # Collect embeddings with cache reuse
        missing_tools = [t for t in tools_list if self._get_embedding(t) is None]
        cache_updated = False
        
        if missing_tools:
            try:
                # Generate embeddings for missing tools
                missing_texts = [f"{t.name}: {t.description}" for t in missing_tools]
                new_embs = self._embedding_fn(missing_texts)
                
                for tool, emb in zip(missing_tools, new_embs, strict=True):
                    self._set_embedding(tool, emb)
                
                cache_updated = True
                logger.debug(f"Computed embeddings for {len(missing_tools)} new tools")
            except Exception as exc:
                logger.error("Failed to generate embeddings: %s", exc)
                return []
        
        # Save to persistent cache if updated
        if cache_updated:
            self._save_persistent_cache()

        try:
            q_emb = self._embedding_fn([query])[0]
        except Exception as exc:
            logger.error("Failed to embed query: %s", exc)
            return []

        scored: list[tuple[BaseTool, float]] = []
        for t in tools_list:
            emb = self._get_embedding(t)
            if emb is None:
                # Should not happen, but handle gracefully
                logger.warning(f"No embedding found for tool: {t.name}")
                scored.append((t, 0.0))
                continue
            
            # Calculate cosine similarity with zero-division protection
            np = _np()
            q_norm = np.linalg.norm(q_emb)
            emb_norm = np.linalg.norm(emb)
            if q_norm == 0 or emb_norm == 0:
                sim = 0.0
            else:
                sim = float(np.dot(q_emb, emb) / (q_norm * emb_norm))
            scored.append((t, sim))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _hybrid_search(
        self, query: str, tools: Iterable[BaseTool], top_k: int
    ) -> List[Tuple[BaseTool, float]]:
        # keyword filter
        kw_top = self._keyword_search(query, tools, top_k * 3)
        if not kw_top:
            # No keyword matches, try semantic search
            semantic_results = self._semantic_search(query, tools, top_k)
            if semantic_results:
                return semantic_results
            # Both failed, return top N tools
            logger.warning("Both keyword and semantic search failed, returning top N tools")
            return [(t, 0.0) for t in list(tools)[:top_k]]
        
        # semantic ranking on keyword results
        semantic_results = self._semantic_search(query, [t for t, _ in kw_top], top_k)
        if semantic_results:
            return semantic_results
        
        # Semantic unavailable, return keyword results
        logger.debug("Semantic search unavailable, using keyword results only")
        return kw_top[:top_k]
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get statistics about the embedding cache.
        
        Returns:
            Dict with structure: {
                "total_embeddings": int,
                "backends": {
                    "backend_name": {
                        "total": int,
                        "servers": {
                            "server_name": int  # count of tools
                        }
                    }
                }
            }
        """
        stats = {
            "total_embeddings": 0,
            "backends": {}
        }
        
        for backend, servers in self._structured_cache.items():
            backend_total = 0
            server_stats = {}
            
            for server, tools in servers.items():
                tool_count = len(tools)
                backend_total += tool_count
                server_stats[server] = tool_count
            
            stats["backends"][backend] = {
                "total": backend_total,
                "servers": server_stats
            }
            stats["total_embeddings"] += backend_total
        
        return stats
    
    def clear_cache(self, backend: Optional[str] = None, server: Optional[str] = None) -> int:
        """Clear embeddings from cache.
        
        Args:
            backend: If provided, only clear this backend. If None, clear all.
            server: If provided (and backend is provided), only clear this server.
        
        Returns:
            Number of embeddings cleared.
        """
        cleared_count = 0
        
        if backend is None:
            # Clear everything
            for b in self._structured_cache.values():
                for s in b.values():
                    cleared_count += len(s)
            self._structured_cache.clear()
            self._text_to_key.clear()
        elif server is None:
            # Clear specific backend
            if backend in self._structured_cache:
                for s in self._structured_cache[backend].values():
                    cleared_count += len(s)
                del self._structured_cache[backend]
                # Rebuild text index
                self._rebuild_text_index()
        else:
            # Clear specific backend+server
            if backend in self._structured_cache and server in self._structured_cache[backend]:
                cleared_count = len(self._structured_cache[backend][server])
                del self._structured_cache[backend][server]
                # Clean up empty backend
                if not self._structured_cache[backend]:
                    del self._structured_cache[backend]
                # Rebuild text index
                self._rebuild_text_index()
        
        # Save after clearing
        if cleared_count > 0 and self._enable_cache_persistence:
            self._save_persistent_cache()
            logger.info(f"Cleared {cleared_count} embeddings from cache")
        
        return cleared_count


class ToolPreselectionDebugInfo:
    """Debug information from the system-side tool preselection process."""
    
    def __init__(self):
        self.search_mode: str = ""
        self.total_candidates: int = 0
        self.deferred_count: int = 0
        self.non_deferred_count: int = 0
        
        # LLM filter info
        self.llm_filter_used: bool = False
        self.llm_brief_plan: str = ""
        self.llm_utility_tools: Dict[str, List[str]] = {}  # server -> tool names
        self.llm_domain_servers: List[str] = []
        self.llm_utility_count: int = 0
        self.llm_domain_count: int = 0
        
        # Semantic search scores
        self.tool_scores: List[Dict[str, Any]] = []  # [{name, server, score, selected}]
        
        # Final selected tools
        self.selected_tools: List[Dict[str, Any]] = []  # [{name, server, backend}]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "search_mode": self.search_mode,
            "total_candidates": self.total_candidates,
            "deferred_count": self.deferred_count,
            "non_deferred_count": self.non_deferred_count,
            "llm_filter": {
                "used": self.llm_filter_used,
                "brief_plan": self.llm_brief_plan,
                "utility_tools": self.llm_utility_tools,
                "domain_servers": self.llm_domain_servers,
                "utility_count": self.llm_utility_count,
                "domain_count": self.llm_domain_count,
            },
            "tool_scores": self.tool_scores,
            "selected_tools": self.selected_tools,
        }


class ToolPreselector(BaseTool):
    """Internal helper: preload and rank tools before schemas are sent.

    This is system-side preselection, not the model-facing ``tool_search``
    discovery tool. ``tool_search`` only discovers deferred schemas during an
    agent turn; this class decides which candidate tools are active up front.
    """

    _name = "_filter_tools"
    _description = "Internal helper: filter & rank tools from a given list."

    _is_read_only = True
    _is_concurrency_safe = True

    # Fallback defaults when config loading fails
    DEFAULT_MAX_TOOLS: int = 20
    DEFAULT_LLM_FILTER: bool = True
    DEFAULT_LLM_THRESHOLD: int = 50
    DEFAULT_CACHE_PERSISTENCE: bool = False
    DEFAULT_SEARCH_MODE: str = "keyword"

    @classmethod
    def get_parameters_schema(cls) -> Dict[str, Any]:
        """Override to avoid JSON schema generation for list[BaseTool] parameter.
        
        The _arun method uses `candidate_tools: list[BaseTool]` which cannot be
        converted to JSON Schema because BaseTool is an ABC class, not a Pydantic model.
        Since this is an internal tool, we return an empty schema.
        """
        return {}

    def __init__(
        self,
        *,
        max_tools: Optional[int] = None,
        llm: "LLMClient | None | object" = _DEFAULT_LLM,
        enable_llm_filter: Optional[bool] = None,
        llm_filter_threshold: Optional[int] = None,
        enable_cache_persistence: Optional[bool] = None,
        cache_dir: Optional[str | Path] = None,
        quality_manager: Optional["ToolQualityManager"] = None,
        enable_quality_ranking: bool = True,
    ):
        """Create a ToolPreselector.

        Args:
            max_tools: max number of tools to return. If None, will use the value from config.
            llm: optional async LLM, used to filter backend/server first
            enable_llm_filter: whether to use LLM to pre-filter by backend/server. 
                If None, uses config value.
            llm_filter_threshold: only apply LLM filter when tool count > this threshold.
                If None, always apply (when enabled).
            enable_cache_persistence: whether to persist embeddings to disk. If None, uses config value.
            cache_dir: directory to store persistent embedding cache. If None, uses config value or default.
        """
        super().__init__()
        
        # Load config (may be None if loading fails)
        tool_search_config = None
        try:
            from openspace.config import get_config
            tool_search_config = getattr(get_config(), 'tool_search', None)
        except Exception as exc:
            logger.warning(f"Failed to load config: {exc}")
        
        def resolve(user_value, config_attr: str, default):
            """Priority: user_value → config → default"""
            if user_value is not None:
                return user_value
            if tool_search_config is not None:
                config_value = getattr(tool_search_config, config_attr, None)
                if config_value is not None:
                    return config_value
            return default
        
        # Resolve each setting with priority: user → config → default
        self.max_tools = resolve(max_tools, 'max_tools', self.DEFAULT_MAX_TOOLS)
        enable_llm_filter = resolve(enable_llm_filter, 'enable_llm_filter', self.DEFAULT_LLM_FILTER)
        llm_filter_threshold = resolve(llm_filter_threshold, 'llm_filter_threshold', self.DEFAULT_LLM_THRESHOLD)
        enable_cache_persistence = resolve(enable_cache_persistence, 'enable_cache_persistence', self.DEFAULT_CACHE_PERSISTENCE)
        cache_dir = resolve(cache_dir, 'cache_dir', None)
        self._default_mode = resolve(None, 'search_mode', self.DEFAULT_SEARCH_MODE)
        
        # Log cache settings for debugging
        logger.info(
            f"ToolPreselector initialized with cache settings: "
            f"enable_cache_persistence={enable_cache_persistence}, cache_dir={cache_dir}"
        )
        
        self._ranker = ToolRanker(
            enable_cache_persistence=enable_cache_persistence,
            cache_dir=cache_dir
        )
        self._use_default_llm = llm is _DEFAULT_LLM
        self._llm: LLMClient | None = None if self._use_default_llm else llm
        
        # LLM filter settings
        self._enable_llm_filter = enable_llm_filter
        self._llm_filter_threshold = llm_filter_threshold
        
        # Quality-aware ranking settings
        self._quality_manager = quality_manager
        self._enable_quality_ranking = enable_quality_ranking
        
        # Debug info from last preselection
        self._last_preselection_debug_info: Optional[ToolPreselectionDebugInfo] = None

    async def _arun(
        self,
        task_prompt: str,
        candidate_tools: list[BaseTool],
        *,
        max_tools: int | None = None,
        mode: str | None = None, # "semantic" | "keyword" | "hybrid"
    ) -> list[BaseTool]:
        max_tools = self.max_tools if max_tools is None else max_tools
        mode = self._default_mode if mode is None else mode

        # Initialize debug info
        debug_info = ToolPreselectionDebugInfo()
        debug_info.search_mode = mode
        debug_info.total_candidates = len(candidate_tools)
        self._last_preselection_debug_info = debug_info

        # Cache check
        cache_key = (id(candidate_tools), task_prompt, mode, max_tools)
        if not hasattr(self, "_query_cache"):
            self._query_cache: Dict[tuple, list[BaseTool]] = {}
        if cache_key in self._query_cache:
            return self._query_cache[cache_key]

        # Split by the single runtime contract: ``tool.is_deferred``.
        # DEC-005: defer decisions are tool-level, not hard-coded to MCP.
        deferred_tools = []
        non_deferred_tools = []
        
        for t in candidate_tools:
            if getattr(t, "is_deferred", False):
                deferred_tools.append(t)
            else:
                non_deferred_tools.append(t)
        
        debug_info.deferred_count = len(deferred_tools)
        debug_info.non_deferred_count = len(non_deferred_tools)
        logger.info(
            "Tool split: %d deferred, %d non-deferred",
            len(deferred_tools),
            len(non_deferred_tools),
        )
        
        # If deferred tools fit within the preload limit, return all.
        if len(deferred_tools) <= max_tools:
            result = deferred_tools + non_deferred_tools
            self._query_cache[cache_key] = result
            self._populate_selected_tools(debug_info, result)
            return result

        deferred_count = len(deferred_tools)
        should_use_llm_filter = (
            self._enable_llm_filter and 
            deferred_count > self._llm_filter_threshold and
            (self._llm is not None or self._use_default_llm)
        )
        
        use_embedding_ranker = mode in {SearchMode.SEMANTIC.value, SearchMode.HYBRID.value}

        # Path 1: LLM pre-filter (large deferred tool set)
        if should_use_llm_filter:
            logger.info(f"Path 1: deferred count ({deferred_count}) > threshold, using LLM filter...")
            debug_info.llm_filter_used = True
            
            try:
                utility_tools, domain_tools, llm_filter_info = await self._llm_filter_with_planning(
                    task_prompt, deferred_tools
                )
                
                # Record LLM filter results
                debug_info.llm_brief_plan = llm_filter_info.get("brief_plan", "")
                debug_info.llm_utility_tools = llm_filter_info.get("utility_tools", {})
                debug_info.llm_domain_servers = llm_filter_info.get("domain_servers", [])
                
                utility_count = len(utility_tools)
                domain_count = len(domain_tools)
                debug_info.llm_utility_count = utility_count
                debug_info.llm_domain_count = domain_count
                total_count = utility_count + domain_count
                
                if total_count <= max_tools:
                    deferred_result = utility_tools + domain_tools
                else:
                    # Exceeds limit: keep utility, search domain
                    domain_quota = max(max_tools - utility_count, 5)
                    logger.info(
                        f"Total ({total_count}) > max_tools ({max_tools}), "
                        f"keeping {utility_count} utility, searching {domain_count} domain (quota: {domain_quota})"
                    )
                    
                    # Compute scores for utility tools (marked as LLM-selected)
                    if utility_tools:
                        utility_ranked = self._rank_tools(
                            task_prompt,
                            utility_tools,
                            top_k=len(utility_tools),
                            mode=mode,
                            use_embedding_ranker=use_embedding_ranker,
                        )
                        self._record_tool_scores(debug_info, utility_ranked, is_selected=True)
                    
                    if domain_tools:
                        # Rank all domain tools to see all scores for debugging
                        all_domain_ranked = self._rank_tools(
                            task_prompt,
                            domain_tools,
                            top_k=len(domain_tools),
                            mode=mode,
                            use_embedding_ranker=use_embedding_ranker,
                        )
                        # Save scores for all domain tools (mark which ones are selected)
                        for i, (tool, score) in enumerate(all_domain_ranked):
                            server_name = None
                            if tool.is_bound and tool.runtime_info:
                                server_name = tool.runtime_info.server_name
                            debug_info.tool_scores.append({
                                "name": tool.name,
                                "server": server_name,
                                "score": round(score, 4),
                                "selected": i < domain_quota,
                            })
                        searched_domain = [t for t, _ in all_domain_ranked[:domain_quota]]
                    else:
                        searched_domain = []
                    
                    deferred_result = utility_tools + searched_domain
                
            except Exception as exc:
                logger.warning(f"LLM filter failed ({exc}), fallback to direct ranking")
                ranked = self._rank_tools(
                    task_prompt,
                    deferred_tools,
                    top_k=max_tools,
                    mode=mode,
                    use_embedding_ranker=use_embedding_ranker,
                )
                self._record_tool_scores(debug_info, ranked, is_selected=True)
                deferred_result = [t for t, _ in ranked]
        
        # Path 2: Lightweight keyword search by default; semantic/hybrid only
        # when explicitly configured.
        else:
            logger.info(f"Path 2: deferred count ({deferred_count}) <= threshold, using {mode} search...")
            debug_info.llm_filter_used = False

            try:
                ranked = self._rank_tools(
                    task_prompt,
                    deferred_tools,
                    top_k=max_tools,
                    mode=mode,
                    use_embedding_ranker=use_embedding_ranker,
                )
                self._record_tool_scores(debug_info, ranked, is_selected=True)
                deferred_result = [t for t, _ in ranked]
            except Exception:
                ranked = rank_tools_by_keyword(task_prompt, deferred_tools, max_results=max_tools)
                self._record_tool_scores(debug_info, ranked, is_selected=True)
                deferred_result = [t for t, _ in ranked]

        # Apply quality ranking on deferred-tool results.
        if self._enable_quality_ranking and self._quality_manager and deferred_result:
            try:
                original_scores = self._latest_tool_score_map(debug_info)
                ranked_with_scores = [
                    (t, original_scores.get(self._tool_score_key(t), 1.0))
                    for t in deferred_result
                ]
                ranked_with_scores = self._quality_manager.adjust_ranking(ranked_with_scores)
                self._record_quality_adjusted_scores(debug_info, ranked_with_scores)
                deferred_result = [t for t, _ in ranked_with_scores]
            except Exception:
                pass

        # Limit deferred tools, then combine with non-deferred tools.
        deferred_result = deferred_result[:max_tools]
        result = deferred_result + non_deferred_tools
        
        # Populate final selected tools in debug info
        self._populate_selected_tools(debug_info, result)
        
        self._log_search_results(candidate_tools, result, mode)
        self._query_cache[cache_key] = result
        return result

    def _rank_tools(
        self,
        query: str,
        tools: list[BaseTool],
        *,
        top_k: int,
        mode: str,
        use_embedding_ranker: bool,
    ) -> list[tuple[BaseTool, float]]:
        if not use_embedding_ranker:
            return [
                (tool, float(score))
                for tool, score in rank_tools_by_keyword(
                    query,
                    tools,
                    max_results=top_k,
                )
            ]
        return self._ranker.rank(query, tools, top_k=top_k, mode=SearchMode(mode))
    
    def _record_tool_scores(
        self, 
        debug_info: ToolPreselectionDebugInfo, 
        ranked: List[Tuple[BaseTool, float]], 
        is_selected: bool = False
    ) -> None:
        """Record tool scores from ranking results."""
        for tool, score in ranked:
            server_name = None
            if tool.is_bound and tool.runtime_info:
                server_name = tool.runtime_info.server_name
            
            debug_info.tool_scores.append({
                "name": tool.name,
                "server": server_name,
                "score": round(score, 4),
                "selected": is_selected,
            })

    def _record_quality_adjusted_scores(
        self,
        debug_info: ToolPreselectionDebugInfo,
        ranked: List[Tuple[BaseTool, float]],
    ) -> None:
        """Attach quality-adjusted scores to existing debug score rows."""

        row_by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
        for row in debug_info.tool_scores:
            row_by_key[(str(row.get("name") or ""), row.get("server"))] = row
        for tool, adjusted_score in ranked:
            key = self._tool_score_key(tool)
            row = row_by_key.get(key)
            if row is None:
                row = {
                    "name": key[0],
                    "server": key[1],
                    "score": 1.0,
                    "selected": True,
                }
                debug_info.tool_scores.append(row)
                row_by_key[key] = row
            base_score = float(row.get("score") or 0.0)
            row["quality_adjusted_score"] = round(adjusted_score, 4)
            if base_score > 0:
                row["quality_penalty"] = round(adjusted_score / base_score, 4)

    @staticmethod
    def _tool_score_key(tool: BaseTool) -> tuple[str, str | None]:
        server_name = None
        if tool.is_bound and tool.runtime_info:
            server_name = tool.runtime_info.server_name
        return (tool.name, server_name)

    @staticmethod
    def _latest_tool_score_map(
        debug_info: ToolPreselectionDebugInfo,
    ) -> dict[tuple[str, str | None], float]:
        scores: dict[tuple[str, str | None], float] = {}
        for row in debug_info.tool_scores:
            name = str(row.get("name") or "")
            if not name:
                continue
            try:
                scores[(name, row.get("server"))] = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
        return scores
    
    def _populate_selected_tools(
        self, 
        debug_info: ToolPreselectionDebugInfo, 
        tools: List[BaseTool]
    ) -> None:
        """Populate selected tools in debug info."""
        for tool in tools:
            backend = "UNKNOWN"
            server_name = None
            
            if tool.is_bound and tool.runtime_info:
                backend = tool.runtime_info.backend.value
                server_name = tool.runtime_info.server_name
            elif tool.backend_type:
                backend = tool.backend_type.value
            
            debug_info.selected_tools.append({
                "name": tool.name,
                "server": server_name,
                "backend": backend,
            })

    async def _llm_filter_with_planning(
        self, 
        task_prompt: str, 
        tools: list[BaseTool]
    ) -> tuple[list[BaseTool], list[BaseTool], Dict[str, Any]]:
        """
        LLM pre-filter for MCP servers.
        Returns (utility_tools, domain_tools, llm_filter_info).
        """
        from collections import defaultdict

        llm = self._get_llm()
        if llm is None:
            return [], tools, {
                "brief_plan": "LLM filtering disabled",
                "utility_tools": {},
                "domain_servers": [],
            }
        
        # Group tools by server name
        server_tools: Dict[str, list[BaseTool]] = defaultdict(list)
        for t in tools:
            if t.is_bound and t.runtime_info:
                server = t.runtime_info.server_name or "default"
            else:
                server = "unknown"
            server_tools[server].append(t)

        # Build tool name -> tool object mapping
        tool_name_map: Dict[str, BaseTool] = {t.name: t for t in tools}

        # Build server description with tool names
        lines: list[str] = ["Available MCP servers:"]
        lines.append("")
        
        for server, tool_list in server_tools.items():
            lines.append(f"### Server: {server} ({len(tool_list)} tools)")
            tool_names = [t.name for t in tool_list]
            lines.append(f"  All tools: {', '.join(tool_names)}")
            if tool_list:
                lines.append(f"  Example capabilities:")
                for tool in tool_list[:5]:
                    tool_desc = tool.description or "No description"
                    if len(tool_desc) > 100:
                        tool_desc = tool_desc[:97] + "..."
                    lines.append(f"    - {tool.name}: {tool_desc}")
            lines.append("")

        servers_block = "\n".join(lines)

        TOOL_FILTER_SYSTEM_PROMPT = f"""You are an expert tool selection assistant.

# Your task
Analyze the given task and determine which MCP servers and tools are needed.
Think about how you would accomplish this task step by step, then classify needed servers and tools.

# Important guidelines
- **Focus on tool names and capabilities**: Carefully examine the tool names to understand what each server can do
- **Be inclusive for domain servers**: If a server has tools that might be relevant to the core task, include it
- **Be precise for utility tools**: Only select the specific auxiliary tools needed (e.g., file save, time query)
- **When in doubt, include in domain_servers**: It's better to include a server than miss relevant tools

{servers_block}

# Output format
Return ONLY a JSON object (no markdown, no explanation):
{{
  "brief_plan": "1-2 sentence execution plan",
  "utility_tools": {{
    "server1": ["tool1", "tool2"]
  }},
  "domain_servers": ["server2", "server3"]
}}

- **utility_tools**: Dict mapping server name to list of specific tool names.
  These are auxiliary tools for supporting operations (e.g., filesystem: ["write_file"], time-server: ["get_time"]).
  Only include the specific tools needed, NOT the entire server.
- **domain_servers**: Server names that directly provide the main capabilities for the task.
  All tools from these servers will be considered. Be inclusive here."""

        user_query = f"Task: {task_prompt}\n\nClassify the needed servers and tools."

        messages = [
            {"role": "system", "content": TOOL_FILTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_query}
        ]
        call_model = getattr(
            llm,
            "call_model_with_fallback",
            llm.call_model,
        )
        resp = await call_model(messages=messages)
        content = resp.assistant_message.get("content", "").strip()
        
        # Extract JSON
        code_block_pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
        match = re.search(code_block_pattern, content, re.DOTALL)
        if match:
            content = match.group(1).strip()
        else:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group()
        
        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response: {e}")
            return [], tools
        
        # Parse utility_tools: {server: [tool_names]}
        utility_tools_config = result.get("utility_tools", {})
        domain_servers = set(result.get("domain_servers", []))
        brief_plan = result.get("brief_plan", "N/A")
        
        logger.info(f"LLM Planning: {brief_plan}")
        logger.info(f"Utility tools: {utility_tools_config}")
        logger.info(f"Domain servers: {domain_servers}")
        
        # Collect utility tools (specific tools only)
        utility_tools = []
        for server_name, tool_names in utility_tools_config.items():
            if server_name in server_tools:
                server_tool_names = {t.name for t in server_tools[server_name]}
                for tool_name in tool_names:
                    if tool_name in server_tool_names and tool_name in tool_name_map:
                        utility_tools.append(tool_name_map[tool_name])
        
        # Collect domain tools (entire servers)
        domain_tools = []
        for server, tool_list in server_tools.items():
            if server in domain_servers:
                domain_tools.extend(tool_list)
        
        logger.info(f"LLM filter result: {len(utility_tools)} utility tools, {len(domain_tools)} domain tools")
        
        # Build LLM filter info for debugging
        llm_filter_info = {
            "brief_plan": brief_plan,
            "utility_tools": utility_tools_config,
            "domain_servers": list(domain_servers),
        }
        
        # Fallback if no match
        if not utility_tools and not domain_tools:
            logger.warning(f"LLM filter matched 0 tools, returning all as domain")
            return [], tools, llm_filter_info
        
        return utility_tools, domain_tools, llm_filter_info

    async def _generate_search_query(self, task_prompt: str) -> str:
        llm = self._get_llm()
        if llm is None:
            return task_prompt

        prompt = f"""Task: {task_prompt}

List keywords for the capabilities needed (comma-separated, brief):"""

        call_model = getattr(
            llm,
            "call_model_with_fallback",
            llm.call_model,
        )
        resp = await call_model(
            messages=[{"role": "user", "content": prompt}]
        )
        capabilities = resp.assistant_message.get("content", "").strip().replace("\n", " ")
        
        enhanced_query = f"{task_prompt} {capabilities}"
        logger.debug(f"Enhanced search query: {enhanced_query[:150]}...")
        
        return enhanced_query

    def _get_llm(self) -> "LLMClient | None":
        if self._llm is None and self._use_default_llm:
            from openspace.llm import LLMClient

            self._llm = LLMClient()
        return self._llm

    def _log_search_results(self, all_tools: list[BaseTool], filtered_tools: list[BaseTool], mode: str) -> None:
        """
        Log search results in a concise, grouped format.
        Shows backend/server breakdown and tool names (truncated if too many).
        """
        from collections import defaultdict
        
        # Group filtered tools by backend and server
        grouped: Dict[str, Dict[str | None, list[str]]] = defaultdict(lambda: defaultdict(list))
        
        for t in filtered_tools:
            # Get backend and server info
            if t.is_bound:
                backend = t.runtime_info.backend.value
                server = t.runtime_info.server_name if backend.lower() == "mcp" else None
            else:
                if not t.backend_type or t.backend_type == BackendType.NOT_SET:
                    backend = "UNKNOWN"
                    server = None
                else:
                    backend = t.backend_type.value
                    server = None
            
            grouped[backend][server].append(t.name)
        
        # Build concise summary
        lines = [f"\n{'='*60}"]
        lines.append(f"🔍 Tool Preselection Results (mode: {mode})")
        lines.append(f"   {len(all_tools)} candidates → {len(filtered_tools)} selected tools")
        lines.append(f"{'='*60}")
        
        for backend, srv_map in sorted(grouped.items()):
            backend_total = sum(len(tools) for tools in srv_map.values())
            lines.append(f"\n📦 {backend} ({backend_total} tools)")
            
            for server, tool_names in sorted(srv_map.items()):
                if backend.lower() == "mcp" and server:
                    prefix = f"   └─ {server}: "
                else:
                    prefix = f"   └─ "
                
                # Limit display to avoid overwhelming output
                if len(tool_names) <= 8:
                    tools_display = ", ".join(tool_names)
                else:
                    tools_display = ", ".join(tool_names[:8]) + f" ... (+{len(tool_names)-8} more)"
                
                lines.append(f"{prefix}{tools_display}")
        
        lines.append(f"{'='*60}\n")
        
        # Use info level so users can see it
        logger.info("\n".join(lines))

    @staticmethod
    def _format_tool_list(tools: list[BaseTool]) -> str:
        rows = [f"{i}. **{t.name}**: {t.description}" for i, t in enumerate(tools, 1)]
        return f"Total {len(tools)} tools, list out directly:\n\n" + "\n".join(rows)

    @staticmethod
    def _format_ranked(results: list[tuple[BaseTool, float]], mode: SearchMode) -> str:
        lines = [f"Search results (mode={mode}) total {len(results)}:\n"]
        for i, (tool, score) in enumerate(results, 1):
            lines.append(f"{i}. {tool.name}  (score: {score:.3f})\n    {tool.description}")
        return "\n".join(lines)

    def _run(self, *args, **kwargs):
        raise NotImplementedError("ToolPreselector only supports asynchronous calls. Use _arun instead.")
    
    def get_embedding_cache_stats(self) -> Dict[str, Any]:
        """Get statistics about the embedding cache.
        
        Returns:
            Dict with cache statistics including total embeddings and breakdown by backend/server.
        """
        return self._ranker.get_cache_stats()
    
    def clear_embedding_cache(self, backend: Optional[str] = None, server: Optional[str] = None) -> int:
        """Clear embeddings from cache.
        
        Args:
            backend: If provided, only clear this backend. If None, clear all.
            server: If provided (and backend is provided), only clear this server.
        
        Returns:
            Number of embeddings cleared.
        """
        return self._ranker.clear_cache(backend=backend, server=server)
    
    def get_last_preselection_debug_info(self) -> Optional[Dict[str, Any]]:
        """Get debug info from the last preselection operation.
        
        Returns:
            Dict containing preselection debug info, or None if no preselection has been performed.
            Includes:
                - search_mode: The search mode used
                - total_candidates: Total number of candidate tools
                - deferred_count/non_deferred_count: Tool counts by defer status
                - llm_filter: LLM filter information if used
                - tool_scores: Similarity scores for each tool
                - selected_tools: Final selected tools
        """
        if self._last_preselection_debug_info is None:
            return None
        return self._last_preselection_debug_info.to_dict()
