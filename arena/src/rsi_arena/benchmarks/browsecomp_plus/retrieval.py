"""Fixed-corpus BM25 ``search`` contract for BrowseComp-Plus.

The backend is an injected seam suitable for a later Pyserini/Lucene adapter.
This module does not build an index, load a model, access the web, or expose a
``get_document`` operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

TOP_K = 5
SNIPPET_MAX_TOKENS = 512


class RetrievalContractError(ValueError):
    """A backend or tokenizer violated the frozen retrieval contract."""


@dataclass(frozen=True)
class BM25DocumentHit:
    """Trusted backend hit before candidate-visible snippet truncation."""

    docid: str
    text: str
    url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.docid, str) or not self.docid.strip():
            raise RetrievalContractError("BM25 hit docid must be non-empty")
        if not isinstance(self.text, str):
            raise RetrievalContractError("BM25 hit text must be a string")
        if self.url is not None and not isinstance(self.url, str):
            raise RetrievalContractError("BM25 hit URL must be a string or None")


def parse_corpus_record(raw: Mapping[str, object]) -> BM25DocumentHit:
    """Strictly parse one official ``docid/text/url`` corpus row."""

    if not isinstance(raw, Mapping) or set(raw) != {"docid", "text", "url"}:
        raise RetrievalContractError(
            "official corpus row must have exactly docid, text, and url"
        )
    if not isinstance(raw["url"], str):
        raise RetrievalContractError("official corpus URL must be a string")
    return BM25DocumentHit(
        docid=raw["docid"],
        text=raw["text"],
        url=raw["url"],
    )


@dataclass(frozen=True)
class SearchResult:
    """The complete candidate-visible result shape."""

    docid: str
    snippet: str
    url: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"docid": self.docid, "snippet": self.snippet, "url": self.url}


@dataclass(frozen=True)
class SearchCall:
    """One successful retrieval call in its original result order."""

    query: str
    results: tuple[SearchResult, ...]

    @property
    def docids(self) -> tuple[str, ...]:
        return tuple(result.docid for result in self.results)

    def as_record(self) -> dict[str, object]:
        return {"query": self.query, "docids": list(self.docids)}

    def as_artifact_record(self) -> dict[str, object]:
        return {
            "query": self.query,
            "results": [result.as_dict() for result in self.results],
        }


@dataclass(frozen=True)
class RetrievalTrajectory:
    """One task's full search-call accounting and first-seen docid union."""

    retrieved_docids: tuple[str, ...]
    tool_call_count: int
    search_calls: tuple[SearchCall, ...] = ()

    def as_record(self) -> dict[str, object]:
        """Return the compact projection safe for attempt/public provenance."""

        return {
            "retrieved_docids": list(self.retrieved_docids),
            "tool_call_count": self.tool_call_count,
        }

    def as_development_record(self) -> dict[str, object]:
        """Return method-visible development search inputs and ordered docids."""

        return {
            **self.as_record(),
            "search_calls": [call.as_record() for call in self.search_calls],
        }

    def snippets_artifact_record(self) -> dict[str, object]:
        """Return the optional full candidate-visible retrieval payload."""

        return {
            "schema": "browsecomp-plus-retrieved-snippets",
            "search_calls": [call.as_artifact_record() for call in self.search_calls],
        }


class BM25SearchBackend(Protocol):
    """Minimal seam implemented by a fixed-corpus BM25 searcher."""

    def search(self, query: str, top_k: int) -> Sequence[BM25DocumentHit]: ...


class Tokenizer(Protocol):
    """Injected tokenizer used to enforce the snippet token ceiling."""

    def encode(self, text: str) -> Sequence[object]: ...

    def decode(self, tokens: Sequence[object]) -> str: ...


class FixedCorpusBM25Retriever:
    """Search-only BM25 wrapper with frozen top-k and snippet token ceiling."""

    def __init__(
        self,
        *,
        backend: BM25SearchBackend,
        tokenizer: Tokenizer,
    ) -> None:
        self._backend = backend
        self._tokenizer = tokenizer
        self._tool_call_count = 0
        self._retrieved_docids: dict[str, None] = {}
        self._search_calls: list[SearchCall] = []

    @property
    def trajectory(self) -> RetrievalTrajectory:
        return RetrievalTrajectory(
            retrieved_docids=tuple(self._retrieved_docids),
            tool_call_count=self._tool_call_count,
            search_calls=tuple(self._search_calls),
        )

    def search(self, query: str) -> tuple[SearchResult, ...]:
        if not isinstance(query, str) or not query.strip():
            raise RetrievalContractError("search query must be a non-empty string")
        self._tool_call_count += 1
        try:
            hits = self._backend.search(query, TOP_K)
            if isinstance(hits, (str, bytes)) or not isinstance(hits, Sequence):
                raise RetrievalContractError("BM25 backend must return a sequence")

            selected = tuple(hits[:TOP_K])
            if any(not isinstance(hit, BM25DocumentHit) for hit in selected):
                raise RetrievalContractError("BM25 backend returned an invalid hit")
            docids = [hit.docid for hit in selected]
            if len(docids) != len(set(docids)):
                raise RetrievalContractError("BM25 backend returned duplicate docids")

            results: list[SearchResult] = []
            for hit in selected:
                encoded = self._tokenizer.encode(hit.text)
                if isinstance(encoded, (str, bytes)) or not isinstance(
                    encoded, Sequence
                ):
                    raise RetrievalContractError(
                        "tokenizer encode must return a sequence"
                    )
                snippet = self._tokenizer.decode(encoded[:SNIPPET_MAX_TOKENS])
                if not isinstance(snippet, str):
                    raise RetrievalContractError(
                        "tokenizer decode must return a string"
                    )
                results.append(
                    SearchResult(docid=hit.docid, snippet=snippet, url=hit.url)
                )
        except Exception:
            self._search_calls.append(SearchCall(query=query, results=()))
            raise
        frozen_results = tuple(results)
        for result in frozen_results:
            self._retrieved_docids.setdefault(result.docid, None)
        self._search_calls.append(SearchCall(query=query, results=frozen_results))
        return frozen_results
