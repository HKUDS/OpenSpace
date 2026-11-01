"""Product-owned physical bindings for BrowseComp-Plus.

This module binds the benchmark interfaces to trusted local files, a
preflighted Pyserini searcher, an operator-provided model router, and an
operator-owned inner-agent runtime factory. It does not download data, build
an index, import candidate code, or call a model provider. Construction opens
the configured local index, validates the complete corpus and tokenizer, and
freezes implementation and resource digests before any candidate runs.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from types import MappingProxyType

from rsi_arena.contracts import canonical_digest
from rsi_arena.inner_agent_runtime import (
    FrozenInnerAgentSelection,
    parse_python_inner_agent_ref,
)

from .catalog import (
    CatalogValidationError,
    build_catalog,
    load_decrypted_official_rows,
    load_official_rows,
)
from .composition import (
    BROWSECOMP_RESOURCE_SCHEMA,
    FrozenBrowseCompPlusBenchmark,
    freeze_browsecomp_plus_benchmark,
    resume_browsecomp_plus_benchmark,
    trusted_catalog_digest,
)
from .contracts import TrustedCatalog
from .evaluator import JudgeRequest
from .harbor_backend import BrowseCompHarborInnerAgentFactory
from .retrieval import (
    SNIPPET_MAX_TOKENS,
    TOP_K,
    BM25DocumentHit,
    RetrievalContractError,
    parse_corpus_record,
)
from .usage import (
    PartialModelUsageError,
    model_usage_calls_from_mapping,
    read_usage_sidecar,
)


class BrowseCompTrustedSetupError(RuntimeError):
    """A trusted dependency or local BrowseComp resource is unavailable."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


class BrowseCompTrustedRuntimeError(RuntimeError):
    """A configured trusted BrowseComp runtime failed after setup."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    try:
        copied = json.loads(json.dumps(dict(value), allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    assert isinstance(copied, dict)
    return copied


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_seconds(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"{label} must be positive")
    return float(value)


def _configured_path(value: object, label: str) -> Path:
    return Path(_required_text(value, label)).expanduser().absolute()


def _regular_file_digest(path: Path, label: str) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BrowseCompTrustedSetupError(
            f"{label}_unavailable", f"trusted BrowseComp {label} is unavailable"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BrowseCompTrustedSetupError(
            f"{label}_invalid", f"trusted BrowseComp {label} must be a regular file"
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise BrowseCompTrustedSetupError(
            f"{label}_unavailable", f"trusted BrowseComp {label} is unreadable"
        ) from exc
    return "sha256:" + digest.hexdigest()


def _tree_digest(path: Path, label: str, *, python_only: bool = False) -> str:
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise BrowseCompTrustedSetupError(
            f"{label}_unavailable", f"trusted BrowseComp {label} is unavailable"
        ) from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise BrowseCompTrustedSetupError(
            f"{label}_invalid", f"trusted BrowseComp {label} must be a real directory"
        )
    entries: list[dict[str, object]] = []
    for child in sorted(
        path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()
    ):
        relative = child.relative_to(path).as_posix()
        try:
            info = child.lstat()
        except OSError as exc:
            raise BrowseCompTrustedSetupError(
                f"{label}_unavailable",
                f"trusted BrowseComp {label} changed during hashing",
            ) from exc
        if stat.S_ISDIR(info.st_mode):
            continue
        if python_only and child.suffix != ".py":
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BrowseCompTrustedSetupError(
                f"{label}_invalid",
                f"trusted BrowseComp {label} contains non-regular material",
            )
        entries.append(
            {
                "path": relative,
                "mode": stat.S_IMODE(info.st_mode),
                "sha256": _regular_file_digest(child, label),
            }
        )
    if not entries:
        raise BrowseCompTrustedSetupError(
            f"{label}_invalid", f"trusted BrowseComp {label} is empty"
        )
    return "sha256:" + canonical_digest(entries)


def _callable_identity(value: object, *, ref: str, label: str) -> Mapping[str, object]:
    try:
        source_path = inspect.getsourcefile(value)
        source = inspect.getsource(value)
    except (OSError, TypeError) as exc:
        raise BrowseCompTrustedSetupError(
            f"{label}_source_unavailable",
            f"configured BrowseComp {label} source is unavailable",
        ) from exc
    if source_path is None:
        raise BrowseCompTrustedSetupError(
            f"{label}_source_unavailable",
            f"configured BrowseComp {label} source is unavailable",
        )
    return MappingProxyType(
        {
            "ref": ref,
            "module_sha256": _regular_file_digest(Path(source_path), label),
            "callable_sha256": "sha256:" + canonical_digest(source),
        }
    )


def _java_identity() -> Mapping[str, object]:
    executable = shutil.which("java")
    if executable is None:
        raise BrowseCompTrustedSetupError(
            "java_unavailable", "Java is required for BrowseComp Pyserini"
        )
    java = Path(executable).resolve(strict=True)
    try:
        completed = subprocess.run(
            [str(java), "-version"],
            capture_output=True,
            timeout=30,
            check=False,
            env={"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrowseCompTrustedSetupError(
            "java_unavailable", "Java runtime preflight failed"
        ) from exc
    output = completed.stderr or completed.stdout
    if completed.returncode != 0 or not output or len(output) > 64 * 1024:
        raise BrowseCompTrustedSetupError(
            "java_unavailable", "Java runtime preflight failed"
        )
    version_match = re.search(
        rb'(?:openjdk|java) version "(?P<major>[0-9]+)(?:[._][^"]*)?"',
        output,
    )
    if version_match is None:
        raise BrowseCompTrustedSetupError(
            "java_unavailable", "Java runtime preflight returned an invalid version"
        )
    major = int(version_match.group("major"))
    if major < 21:
        raise BrowseCompTrustedSetupError(
            "java_version_unsupported",
            "BrowseComp Pyserini requires Java 21 or newer",
        )
    return MappingProxyType(
        {
            "executable_sha256": _regular_file_digest(java, "java_runtime"),
            "major": major,
            "version_sha256": "sha256:" + hashlib.sha256(output).hexdigest(),
        }
    )


def _read_catalog_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BrowseCompTrustedSetupError(
                        "catalog_invalid",
                        f"trusted BrowseComp catalog has invalid JSON at line {line_number}",
                    ) from exc
                if not isinstance(row, Mapping):
                    raise BrowseCompTrustedSetupError(
                        "catalog_invalid",
                        f"trusted BrowseComp catalog row {line_number} is not an object",
                    )
                rows.append(row)
    except BrowseCompTrustedSetupError:
        raise
    except OSError as exc:
        raise BrowseCompTrustedSetupError(
            "catalog_unavailable",
            "trusted BrowseComp catalog path is unavailable",
        ) from exc
    return tuple(rows)


def load_trusted_catalog(
    path: str | os.PathLike[str],
    *,
    catalog_format: str,
) -> TrustedCatalog:
    """Load exactly one explicitly named official JSONL representation."""

    rows = _read_catalog_rows(Path(path).expanduser().absolute())
    try:
        if catalog_format == "official-obfuscated-jsonl":
            tasks = load_official_rows(rows)
        elif catalog_format == "official-decrypted-jsonl":
            tasks = load_decrypted_official_rows(rows)
        else:
            raise ValueError(
                "BrowseComp trusted_data.catalog_format must be "
                "official-obfuscated-jsonl or official-decrypted-jsonl"
            )
        return build_catalog(tasks)
    except CatalogValidationError as exc:
        raise BrowseCompTrustedSetupError(
            "catalog_invalid",
            "trusted BrowseComp catalog violates the official 830-task contract",
        ) from exc


def _load_official_corpus(path: Path) -> Mapping[str, BM25DocumentHit]:
    documents: dict[str, BM25DocumentHit] = {}
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    document = parse_corpus_record(raw)
                except (json.JSONDecodeError, RetrievalContractError) as exc:
                    raise BrowseCompTrustedSetupError(
                        "corpus_invalid",
                        "trusted BrowseComp official corpus is invalid at "
                        f"line {line_number}",
                    ) from exc
                if document.docid in documents:
                    raise BrowseCompTrustedSetupError(
                        "corpus_invalid",
                        "trusted BrowseComp official corpus contains duplicate docids",
                    )
                documents[document.docid] = document
    except BrowseCompTrustedSetupError:
        raise
    except OSError as exc:
        raise BrowseCompTrustedSetupError(
            "corpus_unavailable",
            "trusted BrowseComp corpus path is unavailable",
        ) from exc
    if not documents:
        raise BrowseCompTrustedSetupError(
            "corpus_invalid",
            "trusted BrowseComp official corpus is empty",
        )
    return MappingProxyType(documents)


class LazyPyseriniBM25Backend:
    """One run-scoped corpus authority and Lucene ranker, loaded on first search."""

    def __init__(self, *, index_path: Path, corpus_path: Path) -> None:
        self.index_path = Path(index_path).expanduser().absolute()
        self.corpus_path = Path(corpus_path).expanduser().absolute()
        self._lock = threading.Lock()
        self._searcher: object | None = None
        self._corpus: Mapping[str, BM25DocumentHit] | None = None

    @property
    def constructed(self) -> bool:
        return self._searcher is not None

    def _construct(self) -> tuple[object, Mapping[str, BM25DocumentHit]]:
        with self._lock:
            if self._searcher is not None and self._corpus is not None:
                return self._searcher, self._corpus
            if not self.corpus_path.exists():
                raise BrowseCompTrustedSetupError(
                    "corpus_unavailable",
                    "trusted BrowseComp corpus path is unavailable",
                )
            if not self.index_path.is_dir():
                raise BrowseCompTrustedSetupError(
                    "index_unavailable",
                    "trusted BrowseComp Pyserini index path is unavailable",
                )
            corpus = _load_official_corpus(self.corpus_path)
            try:
                from pyserini.pyclass import autoclass
            except (ImportError, ModuleNotFoundError) as exc:
                raise BrowseCompTrustedSetupError(
                    "pyserini_unavailable",
                    "Pyserini is required for the configured BrowseComp BM25 backend",
                ) from exc
            try:
                # Import only Pyserini's Java bridge.  Its public Lucene package
                # eagerly imports dense/OpenAI encoders that are unrelated to
                # this fixed BM25 backend and would incorrectly require model
                # credentials during a no-model physical preflight.
                simple_searcher = autoclass("io.anserini.search.SimpleSearcher")
                searcher = simple_searcher(str(self.index_path))
            except Exception as exc:
                raise BrowseCompTrustedSetupError(
                    "pyserini_initialization_failed",
                    "BrowseComp Pyserini/Java could not open the configured index",
                ) from exc
            self._searcher = searcher
            self._corpus = corpus
            return searcher, corpus

    def search(self, query: str, top_k: int) -> Sequence[BM25DocumentHit]:
        if top_k != TOP_K:
            raise BrowseCompTrustedRuntimeError(
                "BrowseComp production BM25 must be called with top_k=5"
            )
        searcher, corpus = self._construct()
        try:
            hits = searcher.search(query, top_k)
            results: list[BM25DocumentHit] = []
            for hit in hits:
                docid = getattr(hit, "docid", None)
                if not isinstance(docid, str) or not docid:
                    raise BrowseCompTrustedRuntimeError(
                        "BrowseComp BM25 returned an invalid docid"
                    )
                document = corpus.get(docid)
                if document is None:
                    raise BrowseCompTrustedSetupError(
                        "corpus_docid_missing",
                        "configured BrowseComp index returned a docid absent from "
                        "the official corpus",
                    )
                results.append(document)
            return tuple(results)
        except (BrowseCompTrustedSetupError, BrowseCompTrustedRuntimeError):
            raise
        except Exception as exc:
            raise BrowseCompTrustedRuntimeError(
                "trusted BrowseComp Pyserini search failed"
            ) from exc

    def preflight_resource_identity(self) -> Mapping[str, object]:
        """Load the complete corpus and prove the configured Lucene index opens."""

        self._construct()
        try:
            pyserini_version = metadata.version("pyserini")
        except metadata.PackageNotFoundError as exc:
            raise BrowseCompTrustedSetupError(
                "pyserini_unavailable",
                "installed Pyserini distribution metadata is unavailable",
            ) from exc
        return MappingProxyType(
            {
                "backend": "pyserini-bm25",
                "pyserini": pyserini_version,
                "corpus_sha256": _regular_file_digest(self.corpus_path, "corpus"),
                "index_sha256": _tree_digest(self.index_path, "index"),
            }
        )

    def close(self) -> None:
        with self._lock:
            searcher = self._searcher
            self._searcher = None
            self._corpus = None
        close = getattr(searcher, "close", None)
        if callable(close):
            close()


class LazyTiktokenTokenizer:
    """Configured token boundary without an approximate-token fallback."""

    def __init__(self, encoding_name: str) -> None:
        self.encoding_name = _required_text(
            encoding_name, "BrowseComp retrieval.tokenizer.encoding"
        )
        self._lock = threading.Lock()
        self._encoding: object | None = None

    def _get(self) -> object:
        with self._lock:
            if self._encoding is not None:
                return self._encoding
            try:
                import tiktoken
            except (ImportError, ModuleNotFoundError) as exc:
                raise BrowseCompTrustedSetupError(
                    "tiktoken_unavailable",
                    "tiktoken is required for BrowseComp snippet token limits",
                ) from exc
            try:
                self._encoding = tiktoken.get_encoding(self.encoding_name)
            except Exception as exc:
                raise BrowseCompTrustedSetupError(
                    "tokenizer_unavailable",
                    "configured BrowseComp tokenizer encoding is unavailable",
                ) from exc
            return self._encoding

    def encode(self, text: str) -> Sequence[object]:
        try:
            return self._get().encode(text)
        except BrowseCompTrustedSetupError:
            raise
        except Exception as exc:
            raise BrowseCompTrustedRuntimeError(
                "trusted BrowseComp tokenizer encode failed"
            ) from exc

    def decode(self, tokens: Sequence[object]) -> str:
        try:
            return self._get().decode(list(tokens))
        except BrowseCompTrustedSetupError:
            raise
        except Exception as exc:
            raise BrowseCompTrustedRuntimeError(
                "trusted BrowseComp tokenizer decode failed"
            ) from exc

    def preflight_resource_identity(self) -> Mapping[str, object]:
        encoding = self._get()
        probe = "BrowseComp tokenizer identity π"
        try:
            tokens = encoding.encode(probe)
            decoded = encoding.decode(tokens)
            version = metadata.version("tiktoken")
        except Exception as exc:
            raise BrowseCompTrustedSetupError(
                "tokenizer_unavailable", "BrowseComp tokenizer preflight failed"
            ) from exc
        if decoded != probe:
            raise BrowseCompTrustedSetupError(
                "tokenizer_invalid", "BrowseComp tokenizer round trip changed text"
            )
        return MappingProxyType(
            {
                "kind": "tiktoken",
                "encoding": self.encoding_name,
                "distribution": version,
                "probe_sha256": "sha256:" + canonical_digest(list(tokens)),
            }
        )


class RoutedJudge:
    """Adapt an operator's existing model-router callable to the T1 Judge seam."""

    def __init__(
        self,
        *,
        router_ref: str,
        provider: str,
        model: str,
        profile: Mapping[str, object],
    ) -> None:
        parsed = parse_python_inner_agent_ref(router_ref)
        if parsed is None:
            raise ValueError(
                "BrowseComp judge.router_ref must be python:<module>:<callable>"
            )
        self._module = parsed.module
        self._callable = parsed.callable
        self._provider = _required_text(provider, "BrowseComp judge.provider")
        self._model = _required_text(model, "BrowseComp judge.model")
        self._profile = MappingProxyType(_object(profile, "BrowseComp judge.profile"))
        self._lock = threading.Lock()
        self._router: object | None = None

    def _resolve(self):
        with self._lock:
            if self._router is not None:
                return self._router
            try:
                module = importlib.import_module(self._module)
                router = getattr(module, self._callable)
            except (ImportError, AttributeError) as exc:
                raise BrowseCompTrustedSetupError(
                    "judge_router_unavailable",
                    "configured BrowseComp judge router callable is unavailable",
                ) from exc
            if not callable(router):
                raise BrowseCompTrustedSetupError(
                    "judge_router_unavailable",
                    "configured BrowseComp judge router is not callable",
                )
            try:
                inspect.signature(router).bind(
                    prompt=object(),
                    provider=object(),
                    model=object(),
                    profile=object(),
                )
            except (TypeError, ValueError) as exc:
                raise BrowseCompTrustedSetupError(
                    "judge_router_invalid",
                    "BrowseComp judge router must accept prompt, provider, model, and profile",
                ) from exc
            self._router = router
            return router

    def __call__(self, request: JudgeRequest) -> object:
        if not isinstance(request, JudgeRequest):
            raise TypeError("BrowseComp judge requires a JudgeRequest")
        return self._resolve()(
            prompt=request.prompt,
            provider=self._provider,
            model=self._model,
            profile=self._profile,
        )

    def preflight_resource_identity(self) -> Mapping[str, object]:
        router = self._resolve()
        return MappingProxyType(
            {
                "implementation": dict(
                    _callable_identity(
                        router,
                        ref=f"python:{self._module}:{self._callable}",
                        label="judge_router",
                    )
                ),
                "provider": self._provider,
                "model": self._model,
                "profile_sha256": "sha256:" + canonical_digest(dict(self._profile)),
            }
        )


class RoutedInnerAgentFactory:
    """Delegate candidate execution to an existing trusted runtime factory.

    The configured runner is operator-owned.  It receives the frozen candidate
    capability and the existing inner-agent/model selection, and returns the
    thin ``context/model_runtime/config`` proxy invoked by the T2 backend.  This
    package never imports or executes candidate modules in the trusted process.
    """

    def __init__(
        self,
        *,
        runner_ref: str,
        selection: FrozenInnerAgentSelection,
        model_runtime: Mapping[str, object],
    ) -> None:
        parsed = parse_python_inner_agent_ref(runner_ref)
        if parsed is None:
            raise ValueError(
                "BrowseComp agent_runtime.runner_ref must be python:<module>:<callable>"
            )
        self._module = parsed.module
        self._callable = parsed.callable
        self._selection = selection
        self._model_runtime = MappingProxyType(
            _object(model_runtime, "BrowseComp agent_model")
        )
        self._lock = threading.Lock()
        self._runner: object | None = None
        self._runner_identity: Mapping[str, object] | None = None

    @property
    def publishes_trusted_usage(self) -> bool:
        return True

    def _resolve(self):
        with self._lock:
            if self._runner is not None:
                return self._runner
            try:
                module = importlib.import_module(self._module)
                runner = getattr(module, self._callable)
            except (ImportError, AttributeError) as exc:
                raise BrowseCompTrustedSetupError(
                    "agent_runtime_unavailable",
                    "configured BrowseComp agent runtime factory is unavailable",
                ) from exc
            if not callable(runner):
                raise BrowseCompTrustedSetupError(
                    "agent_runtime_unavailable",
                    "configured BrowseComp agent runtime factory is not callable",
                )
            source_path = inspect.getsourcefile(runner)
            if source_path is None:
                raise BrowseCompTrustedSetupError(
                    "agent_runtime_invalid",
                    "configured BrowseComp agent runtime source is unavailable",
                )
            module_path = Path(source_path).resolve(strict=True)
            callable_identity = dict(
                _callable_identity(
                    runner,
                    ref=f"python:{self._module}:{self._callable}",
                    label="agent_runner",
                )
            )
            self._runner_identity = MappingProxyType(
                {
                    **callable_identity,
                    "module_path": str(module_path),
                    "closure_root": str(module_path.parent),
                    "closure_sha256": _tree_digest(
                        module_path.parent, "agent_runner_closure", python_only=True
                    ),
                }
            )
            try:
                inspect.signature(runner).bind(
                    candidate_source=object(),
                    agent_ref=object(),
                    model_runtime=object(),
                    config=object(),
                )
            except (TypeError, ValueError) as exc:
                raise BrowseCompTrustedSetupError(
                    "agent_runtime_invalid",
                    "BrowseComp agent runtime factory must accept candidate_source, "
                    "agent_ref, model_runtime, and config",
                ) from exc
            self._runner = runner
            return runner

    def __call__(self, candidate_source: object):
        return self._resolve()(
            candidate_source=candidate_source,
            agent_ref=self._selection.ref,
            model_runtime=self._model_runtime,
            config=self._selection.config,
        )

    def run_with_wall_timeout(
        self,
        candidate_source: object,
        *,
        context: object,
        model_runtime: Mapping[str, object],
        config: Mapping[str, object],
        timeout_seconds: float,
    ) -> object:
        """Execute the routed runner in a killable, process-isolated boundary."""

        if dict(model_runtime) != dict(self._model_runtime) or dict(config) != dict(
            self._selection.config
        ):
            raise BrowseCompTrustedRuntimeError(
                "BrowseComp routed runtime inputs changed after construction"
            )
        checkout = getattr(candidate_source, "checkout_path", candidate_source)
        instruction = getattr(context, "instruction", None)
        environment = getattr(context, "environment", None)
        search_endpoint = getattr(environment, "search_endpoint", None)
        if not isinstance(checkout, (str, os.PathLike)):
            raise BrowseCompTrustedRuntimeError(
                "BrowseComp routed candidate checkout is unavailable"
            )
        if not isinstance(instruction, str) or not isinstance(search_endpoint, str):
            raise BrowseCompTrustedRuntimeError(
                "BrowseComp routed task context is unavailable"
            )
        request = {
            "runner_module": self._module,
            "runner_callable": self._callable,
            "candidate_checkout": str(Path(checkout).resolve(strict=True)),
            "agent_ref": self._selection.ref,
            "model_runtime": dict(self._model_runtime),
            "config": dict(self._selection.config),
            "benchmark_id": "browsecomp_plus",
            "instruction": instruction,
            "search_endpoint": search_endpoint,
        }
        with tempfile.TemporaryDirectory(prefix="browsecomp-runner-") as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            result_path = root / "result.json"
            usage_path = root / "usage.jsonl"
            self._resolve()
            if self._runner_identity is None:  # pragma: no cover - bound by _resolve
                raise BrowseCompTrustedRuntimeError(
                    "BrowseComp runner identity is unavailable"
                )
            request["usage_sidecar"] = str(usage_path)
            request["runner_identity"] = dict(self._runner_identity)
            request_path.write_text(
                json.dumps(request, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            request_path.chmod(0o600)
            process = subprocess.Popen(
                (
                    sys.executable,
                    "-m",
                    "rsi_arena.benchmarks.browsecomp_plus.runner_worker",
                    str(request_path),
                    str(result_path),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={
                    **os.environ,
                    "PYTHONNOUSERSITE": "1",
                },
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2.0)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                raise PartialModelUsageError(
                    "BrowseComp routed runner exceeded its task wall timeout",
                    read_usage_sidecar(usage_path),
                ) from exc
            if process.returncode != 0:
                raise PartialModelUsageError(
                    "BrowseComp routed runner process failed",
                    read_usage_sidecar(usage_path),
                )
            try:
                info = result_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise BrowseCompTrustedRuntimeError(
                        "BrowseComp routed runner result is invalid"
                    )
                if info.st_size > 10 * 1024 * 1024:
                    raise BrowseCompTrustedRuntimeError(
                        "BrowseComp routed runner result exceeds its byte limit"
                    )
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise BrowseCompTrustedRuntimeError(
                    "BrowseComp routed runner result is unreadable"
                ) from exc
            if not isinstance(payload, Mapping) or set(payload) != {"ok", "result"}:
                raise BrowseCompTrustedRuntimeError(
                    "BrowseComp routed runner result is malformed"
                )
            if payload["ok"] is not True:
                raise BrowseCompTrustedRuntimeError(
                    "BrowseComp routed runner returned an error"
                )
            result = payload["result"]
            if not isinstance(result, Mapping):
                raise BrowseCompTrustedRuntimeError(
                    "BrowseComp routed runner result is malformed"
                )
            sidecar_calls = read_usage_sidecar(usage_path)
            if sidecar_calls:
                reported_calls = model_usage_calls_from_mapping(result.get("usage"))
                if reported_calls and reported_calls != sidecar_calls:
                    raise BrowseCompTrustedRuntimeError(
                        "BrowseComp runner usage differs from its durable sidecar"
                    )
                result = {
                    **dict(result),
                    "usage": {"calls": [call.as_dict() for call in sidecar_calls]},
                }
            return result

    def preflight_resource_identity(self) -> Mapping[str, object]:
        runner = self._resolve()
        del runner
        if self._runner_identity is None:  # pragma: no cover - bound by _resolve
            raise BrowseCompTrustedSetupError(
                "agent_runtime_invalid", "agent runner identity is unavailable"
            )
        return MappingProxyType(
            {
                "kind": "python-runner",
                "implementation": {
                    name: value
                    for name, value in self._runner_identity.items()
                    if name not in {"module_path", "closure_root"}
                },
                "selection": dict(self._selection.identity),
                "model_runtime_sha256": "sha256:"
                + canonical_digest(dict(self._model_runtime)),
            }
        )


_FORBIDDEN_HARBOR_AGENT_CONFIG = frozenset(
    {
        "api_key",
        "artifact_path",
        "artifact_source",
        "cwd",
        "final_answer_path",
        "password",
        "path",
        "payload_name",
        "source",
        "token",
        "credential",
        "credential_value",
        "workdir",
    }
)


def _harbor_inner_agent_factory(
    *,
    agent_runtime: Mapping[str, object],
    agent_model: Mapping[str, object],
    inner_agent_selection: FrozenInnerAgentSelection,
    control_root: Path | None,
) -> BrowseCompHarborInnerAgentFactory:
    if "runner_ref" in agent_runtime:
        raise ValueError(
            "BrowseComp builtin:terminus-2 rejects runner_ref/harbor dual authority"
        )
    if control_root is None:
        raise ValueError("BrowseComp Terminus production requires control_root")
    forbidden = sorted(
        _FORBIDDEN_HARBOR_AGENT_CONFIG.intersection(inner_agent_selection.config)
    )
    if forbidden:
        raise ValueError(
            "BrowseComp inner_agent.config cannot override package-owned paths: "
            + ", ".join(forbidden)
        )
    harbor = _object(agent_runtime.get("harbor", {}), "BrowseComp agent_runtime.harbor")
    secret_fields = sorted(
        {"api_key", "credential", "credential_value", "password", "token"}
        & (set(harbor) | set(agent_model))
    )
    if secret_fields:
        raise ValueError(
            "BrowseComp credentials must be supplied only by environment name: "
            + ", ".join(secret_fields)
        )
    return BrowseCompHarborInnerAgentFactory(
        control_root=(
            Path(control_root) / "benchmark-runtime" / "browsecomp_plus" / "harbor"
        ),
        trusted_harbor_root=_configured_path(
            harbor.get("trusted_harbor_root"),
            "BrowseComp agent_runtime.harbor.trusted_harbor_root",
        ),
        dind_bind_root=_configured_path(
            harbor.get("dind_bind_root"),
            "BrowseComp agent_runtime.harbor.dind_bind_root",
        ),
        task_image=_required_text(
            harbor.get("task_image"),
            "BrowseComp agent_runtime.harbor.task_image",
        ),
        model_runtime=agent_model,
        agent_config=inner_agent_selection.config,
        credential_env_name=_required_text(
            harbor.get("credential_env_name"),
            "BrowseComp agent_runtime.harbor.credential_env_name",
        ),
        provider_base_url=_required_text(
            harbor.get("provider_base_url"),
            "BrowseComp agent_runtime.harbor.provider_base_url",
        ),
        docker_path=str(harbor.get("docker_path", "docker")),
        bwrap_path=str(harbor.get("bwrap_path", "bwrap")),
        worker_timeout_seconds=harbor.get("worker_timeout_seconds", 14_400.0),
        harbor_wall_timeout_seconds=harbor.get("harbor_wall_timeout_seconds", 15_300.0),
        cleanup_grace_seconds=harbor.get("cleanup_grace_seconds", 30.0),
    )


def _physical_components(
    config: Mapping[str, object],
    *,
    inner_agent_selection: FrozenInnerAgentSelection,
    control_root: Path | None = None,
) -> tuple[
    TrustedCatalog,
    LazyPyseriniBM25Backend,
    LazyTiktokenTokenizer,
    RoutedJudge,
    object,
    Mapping[str, object],
    Mapping[str, object],
    int,
    float,
]:
    trusted_data = _object(config.get("trusted_data", {}), "BrowseComp trusted_data")
    catalog_path = _configured_path(
        trusted_data.get("catalog_path"), "BrowseComp trusted_data.catalog_path"
    )
    corpus_path = _configured_path(
        trusted_data.get("corpus_path"), "BrowseComp trusted_data.corpus_path"
    )
    index_path = _configured_path(
        trusted_data.get("index_path"), "BrowseComp trusted_data.index_path"
    )
    catalog_format = _required_text(
        trusted_data.get("catalog_format"),
        "BrowseComp trusted_data.catalog_format",
    )

    retrieval = _object(config.get("retrieval", {}), "BrowseComp retrieval")
    if retrieval.get("backend") != "pyserini-bm25":
        raise ValueError("BrowseComp retrieval.backend must be pyserini-bm25")
    if retrieval.get("top_k") != TOP_K:
        raise ValueError("BrowseComp retrieval.top_k must be exactly 5")
    if retrieval.get("snippet_tokens") != SNIPPET_MAX_TOKENS:
        raise ValueError("BrowseComp retrieval.snippet_tokens must be exactly 512")
    search_concurrency = _positive_integer(
        retrieval.get("search_concurrency"),
        "BrowseComp retrieval.search_concurrency",
    )
    tokenizer = _object(
        retrieval.get("tokenizer", {}), "BrowseComp retrieval.tokenizer"
    )
    if tokenizer.get("kind") != "tiktoken":
        raise ValueError("BrowseComp retrieval.tokenizer.kind must be tiktoken")
    encoding = _required_text(
        tokenizer.get("encoding"), "BrowseComp retrieval.tokenizer.encoding"
    )

    judge = _object(config.get("judge", {}), "BrowseComp judge")
    judge_profile = _object(judge.get("profile", {}), "BrowseComp judge.profile")
    routed_judge = RoutedJudge(
        router_ref=_required_text(
            judge.get("router_ref"), "BrowseComp judge.router_ref"
        ),
        provider=_required_text(judge.get("provider"), "BrowseComp judge.provider"),
        model=_required_text(judge.get("model"), "BrowseComp judge.model"),
        profile=judge_profile,
    )
    agent_model = _object(config.get("agent_model", {}), "BrowseComp agent_model")
    agent_runtime = _object(config.get("agent_runtime", {}), "BrowseComp agent_runtime")
    if inner_agent_selection.ref == "builtin:terminus-2":
        agent_factory: object = _harbor_inner_agent_factory(
            agent_runtime=agent_runtime,
            agent_model=agent_model,
            inner_agent_selection=inner_agent_selection,
            control_root=control_root,
        )
    else:
        agent_factory = RoutedInnerAgentFactory(
            runner_ref=_required_text(
                agent_runtime.get("runner_ref"),
                "BrowseComp agent_runtime.runner_ref",
            ),
            selection=inner_agent_selection,
            model_runtime=agent_model,
        )
    task_wall_timeout_seconds = (
        agent_factory.harbor_wall_timeout_seconds
        if isinstance(agent_factory, BrowseCompHarborInnerAgentFactory)
        else _positive_seconds(
            agent_runtime.get("task_wall_timeout_seconds", 3_600.0),
            "BrowseComp agent_runtime.task_wall_timeout_seconds",
        )
    )
    catalog = load_trusted_catalog(catalog_path, catalog_format=catalog_format)
    return (
        catalog,
        LazyPyseriniBM25Backend(index_path=index_path, corpus_path=corpus_path),
        LazyTiktokenTokenizer(encoding),
        routed_judge,
        agent_factory,
        MappingProxyType(agent_model),
        inner_agent_selection.config,
        search_concurrency,
        task_wall_timeout_seconds,
    )


def _preflight_resource_identity(
    config: Mapping[str, object],
    *,
    catalog: TrustedCatalog,
    backend: LazyPyseriniBM25Backend,
    tokenizer: LazyTiktokenTokenizer,
    judge: RoutedJudge,
    agent_factory: object,
    inner_agent_selection: FrozenInnerAgentSelection,
) -> Mapping[str, object]:
    trusted_data = _object(config.get("trusted_data", {}), "BrowseComp trusted_data")
    catalog_path = _configured_path(
        trusted_data.get("catalog_path"), "BrowseComp trusted_data.catalog_path"
    )
    agent_identity = getattr(agent_factory, "preflight_resource_identity", None)
    if not callable(agent_identity):
        raise BrowseCompTrustedSetupError(
            "agent_runtime_identity_unavailable",
            "BrowseComp agent runtime has no resource identity preflight",
        )
    java_identity = _java_identity()
    return MappingProxyType(
        {
            "schema": BROWSECOMP_RESOURCE_SCHEMA,
            "execution_configuration_sha256": "sha256:"
            + canonical_digest(
                {
                    name: config.get(name, {})
                    for name in (
                        "trusted_data",
                        "retrieval",
                        "judge",
                        "agent_model",
                        "agent_runtime",
                    )
                }
            ),
            "catalog": {
                "file_sha256": _regular_file_digest(catalog_path, "catalog"),
                "trusted_content_sha256": trusted_catalog_digest(catalog),
            },
            "retrieval": {
                **dict(backend.preflight_resource_identity()),
                "java": dict(java_identity),
                "tokenizer": dict(tokenizer.preflight_resource_identity()),
            },
            "judge": dict(judge.preflight_resource_identity()),
            "agent": dict(agent_identity()),
            "inner_agent_selection": dict(inner_agent_selection.identity),
            "package_source_sha256": _tree_digest(
                Path(__file__).resolve().parent,
                "package_source",
                python_only=True,
            ),
        }
    )


def freeze_product_browsecomp_plus_benchmark(
    config: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
    publish_development_judge_text: bool = False,
) -> FrozenBrowseCompPlusBenchmark:
    """Build the product package without touching Pyserini, Java, or a model API."""

    (
        catalog,
        backend,
        tokenizer,
        judge,
        agent_factory,
        model_runtime,
        agent_config,
        search_concurrency,
        task_wall_timeout_seconds,
    ) = _physical_components(
        config,
        inner_agent_selection=inner_agent_selection,
        control_root=control_root,
    )
    try:
        resources = _preflight_resource_identity(
            config,
            catalog=catalog,
            backend=backend,
            tokenizer=tokenizer,
            judge=judge,
            agent_factory=agent_factory,
            inner_agent_selection=inner_agent_selection,
        )
        return freeze_browsecomp_plus_benchmark(
            config,
            control_root=control_root,
            catalog=catalog,
            search_backend=backend,
            tokenizer=tokenizer,
            judge=judge,
            inner_agent_factory=agent_factory,
            model_runtime=model_runtime,
            agent_config=agent_config,
            retrieval_concurrency=search_concurrency,
            publish_development_judge_text=publish_development_judge_text,
            retrieval_socket_parent=getattr(
                agent_factory, "search_socket_parent", None
            ),
            resource_identity=resources,
            task_wall_timeout_seconds=task_wall_timeout_seconds,
        )
    except BaseException:
        backend.close()
        close = getattr(agent_factory, "close", None)
        if callable(close):
            close()
        raise


def resume_product_browsecomp_plus_benchmark(
    config: Mapping[str, object],
    resolved_identity: Mapping[str, object],
    *,
    control_root: Path,
    inner_agent_selection: FrozenInnerAgentSelection,
    publish_development_judge_text: bool = False,
) -> FrozenBrowseCompPlusBenchmark:
    (
        catalog,
        backend,
        tokenizer,
        judge,
        agent_factory,
        model_runtime,
        agent_config,
        search_concurrency,
        task_wall_timeout_seconds,
    ) = _physical_components(
        config,
        inner_agent_selection=inner_agent_selection,
        control_root=control_root,
    )
    try:
        resources = _preflight_resource_identity(
            config,
            catalog=catalog,
            backend=backend,
            tokenizer=tokenizer,
            judge=judge,
            agent_factory=agent_factory,
            inner_agent_selection=inner_agent_selection,
        )
        return resume_browsecomp_plus_benchmark(
            config,
            resolved_identity,
            control_root=control_root,
            catalog=catalog,
            search_backend=backend,
            tokenizer=tokenizer,
            judge=judge,
            inner_agent_factory=agent_factory,
            model_runtime=model_runtime,
            agent_config=agent_config,
            retrieval_concurrency=search_concurrency,
            publish_development_judge_text=publish_development_judge_text,
            retrieval_socket_parent=getattr(
                agent_factory, "search_socket_parent", None
            ),
            resource_identity=resources,
            task_wall_timeout_seconds=task_wall_timeout_seconds,
        )
    except BaseException:
        backend.close()
        close = getattr(agent_factory, "close", None)
        if callable(close):
            close()
        raise


__all__ = [
    "BrowseCompTrustedRuntimeError",
    "BrowseCompTrustedSetupError",
    "LazyPyseriniBM25Backend",
    "LazyTiktokenTokenizer",
    "RoutedInnerAgentFactory",
    "RoutedJudge",
    "freeze_product_browsecomp_plus_benchmark",
    "load_trusted_catalog",
    "resume_product_browsecomp_plus_benchmark",
]
