"""Hardened one-file-per-occurrence storage for normalized Visible-A traces."""

from __future__ import annotations

import errno
import os
import stat
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from agentpilot.domain.common import canonical_json
from agentpilot.domain.trace_evidence import (
    AdapterTraceCapability,
    NormalizedTrace,
    ReadResult,
    TraceArtifactRef,
    TraceCaptureCoordinates,
    TraceCaptureRecord,
    TraceCaptureResult,
    TraceCaptureStatus,
    TraceCaseIndex,
    TraceEvidenceManifest,
    decode_normalized_trace,
    summarize_trace_actions,
)


DEFAULT_MAX_TRACE_BYTES = 256 * 1024 * 1024


class TraceStoreError(RuntimeError):
    """Base class for bounded trace-store failures."""


class TraceStoreSecurityError(TraceStoreError):
    """A path or filesystem object violated the immutable store boundary."""


class TraceStoreIntegrityError(TraceStoreError):
    """Stored bytes or metadata violate the immutable trace contract."""


class TraceStoreLookupError(TraceStoreError):
    """A logical trace, case or manifest is not registered in this view."""


def _positive_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 < value <= maximum:
        raise ValueError(f"{name} must be in [1, {maximum}]")
    return value


def _ensure_private_directory(path: Path) -> None:
    """Create one trusted store directory without accepting a symlink."""

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise TraceStoreSecurityError(
            f"cannot create trace store directory: {path}"
        ) from exc
    try:
        value = path.lstat()
    except OSError as exc:
        raise TraceStoreSecurityError(
            f"cannot inspect trace store directory: {path}"
        ) from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise TraceStoreSecurityError(
            f"trace store path is not a real directory: {path}"
        )
    if value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise TraceStoreSecurityError(
            f"trace store directory must be owner-only: {path}"
        )


class TraceStore:
    """One run's immutable trace files and logical read view."""

    def __init__(
        self,
        run_root: str | os.PathLike[str],
        *,
        max_trace_bytes: int = DEFAULT_MAX_TRACE_BYTES,
    ) -> None:
        self.max_trace_bytes = _positive_int(
            max_trace_bytes, "max_trace_bytes", maximum=1_000_000_000
        )
        root = Path(run_root)
        if not root.is_absolute():
            root = Path.cwd() / root
        # ``absolute`` normalizes no symlinks; the checks below must see them.
        self.run_root = root.absolute()
        _ensure_private_directory(self.run_root)
        evidence = self.run_root / "evidence"
        _ensure_private_directory(evidence)
        self.trace_root = evidence / "traces"
        _ensure_private_directory(self.trace_root)
        self._lock = threading.RLock()
        self._artifact_refs: dict[str, TraceArtifactRef] = {}
        self._case_indexes: dict[str, TraceCaseIndex] = {}
        self._manifests: dict[str, TraceEvidenceManifest] = {}

    def _open_trace_directory(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.trace_root, flags)
        except OSError as exc:
            raise TraceStoreSecurityError(
                "trace directory is unavailable"
            ) from exc
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            os.close(descriptor)
            raise TraceStoreSecurityError("trace root is not a directory")
        return descriptor

    @staticmethod
    def _file_name(trace_ref_id: str) -> str:
        if (
            not isinstance(trace_ref_id, str)
            or not trace_ref_id.startswith("trace-ref-")
            or len(trace_ref_id) > 128
            or any(
                not (character.isalnum() or character in "-_")
                for character in trace_ref_id
            )
        ):
            raise TraceStoreSecurityError("invalid trace ref")
        return trace_ref_id

    def put(self, trace_ref_id: str, trace: NormalizedTrace) -> str:
        """Store one canonical trace under its occurrence ref."""

        if not isinstance(trace, NormalizedTrace):
            raise TypeError("trace must be NormalizedTrace")
        return self.put_bytes(trace_ref_id, trace.canonical_bytes())

    def put_bytes(self, trace_ref_id: str, payload: bytes) -> str:
        """Validate and atomically publish one immutable occurrence file."""

        file_name = self._file_name(trace_ref_id)
        if not isinstance(payload, bytes):
            raise TypeError("trace payload must be bytes")
        _positive_int(
            len(payload), "trace payload byte count", maximum=self.max_trace_bytes
        )
        decode_normalized_trace(payload)
        directory = self._open_trace_directory()
        temporary = f".trace-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("trace store write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary,
                    file_name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            except FileExistsError:
                existing = self._read_from_directory(directory, file_name)
                if existing != payload:
                    raise TraceStoreIntegrityError(
                        "trace ref already stores different canonical bytes"
                    )
            except OSError as exc:
                raise TraceStoreError("cannot publish normalized trace") from exc
        except TraceStoreError:
            raise
        except OSError as exc:
            raise TraceStoreError("cannot write normalized trace") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            except OSError:
                # A leftover private temp never becomes addressable evidence.
                pass
            os.close(directory)
        return trace_ref_id

    def _read_from_directory(self, directory: int, file_name: str) -> bytes:
        try:
            value = os.stat(file_name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise TraceStoreLookupError(f"trace not found: {file_name}") from exc
        except OSError as exc:
            raise TraceStoreError("cannot inspect trace") from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise TraceStoreSecurityError("trace is not a regular file")
        if value.st_size <= 0 or value.st_size > self.max_trace_bytes:
            raise TraceStoreIntegrityError("trace violates its byte bound")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(file_name, flags, dir_fd=directory)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise TraceStoreSecurityError("trace is a symlink") from exc
            raise TraceStoreError("cannot open trace") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev != value.st_dev or opened.st_ino != value.st_ino
            ):
                raise TraceStoreSecurityError("trace changed while opening")
            chunks: list[bytes] = []
            remaining = self.max_trace_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(payload) != opened.st_size:
            raise TraceStoreIntegrityError("trace changed while reading")
        return payload

    def get_bytes(self, trace_ref_id: str) -> bytes:
        file_name = self._file_name(trace_ref_id)
        directory = self._open_trace_directory()
        try:
            payload = self._read_from_directory(directory, file_name)
        finally:
            os.close(directory)
        try:
            decode_normalized_trace(payload)
        except ValueError as exc:
            raise TraceStoreIntegrityError(
                "stored trace is not canonical normalized evidence"
            ) from exc
        return payload

    def get(self, trace_ref_id: str) -> NormalizedTrace:
        return decode_normalized_trace(self.get_bytes(trace_ref_id))

    def register_artifact(self, artifact: TraceArtifactRef) -> None:
        """Register one logical occurrence for ref-based reads."""

        self._verify_artifact(artifact)
        with self._lock:
            prior = self._artifact_refs.get(artifact.trace_ref_id)
            if prior is not None and prior != artifact:
                raise TraceStoreIntegrityError(
                    "trace occurrence identity was registered with different content"
                )
            self._artifact_refs[artifact.trace_ref_id] = artifact

    def _verify_artifact(self, artifact: TraceArtifactRef) -> None:
        """Check physical bytes without mutating the logical read view."""

        if not isinstance(artifact, TraceArtifactRef):
            raise TypeError("artifact must be TraceArtifactRef")
        # Verify physical integrity before the occurrence becomes readable.
        payload = self.get_bytes(artifact.trace_ref_id)
        trace = decode_normalized_trace(payload)
        if (
            artifact.byte_count != len(payload)
            or artifact.event_count != len(trace.events)
            or artifact.trace_contract != trace.trace_contract
        ):
            raise TraceStoreIntegrityError(
                "trace artifact metadata differs from its normalized payload"
            )

    def register_manifest(self, manifest: TraceEvidenceManifest) -> None:
        """Register the exact persisted manifest as an immutable read view."""

        if not isinstance(manifest, TraceEvidenceManifest):
            raise TypeError("manifest must be TraceEvidenceManifest")
        for artifact in manifest.artifact_refs:
            self._verify_artifact(artifact)
        with self._lock:
            prior = self._manifests.get(manifest.manifest_id)
            if prior is not None and prior != manifest:
                raise TraceStoreIntegrityError(
                    "trace manifest identity was registered with different content"
                )
            for case in manifest.case_indexes:
                prior_case = self._case_indexes.get(case.case_ref)
                if prior_case is not None and prior_case != case:
                    raise TraceStoreIntegrityError(
                        "trace case identity was registered with different content"
                    )
            for artifact in manifest.artifact_refs:
                prior_artifact = self._artifact_refs.get(artifact.trace_ref_id)
                if prior_artifact is not None and prior_artifact != artifact:
                    raise TraceStoreIntegrityError(
                        "trace occurrence identity was registered with different content"
                    )
            self._artifact_refs.update(
                {item.trace_ref_id: item for item in manifest.artifact_refs}
            )
            self._manifests[manifest.manifest_id] = manifest
            self._case_indexes.update(
                {case.case_ref: case for case in manifest.case_indexes}
            )

    def artifact(self, trace_ref_id: str) -> TraceArtifactRef:
        with self._lock:
            try:
                return self._artifact_refs[trace_ref_id]
            except KeyError as exc:
                raise TraceStoreLookupError(
                    f"trace occurrence not registered: {trace_ref_id}"
                ) from exc

    def manifest(self, manifest_id: str) -> TraceEvidenceManifest:
        with self._lock:
            try:
                return self._manifests[manifest_id]
            except KeyError as exc:
                raise TraceStoreLookupError(
                    f"trace manifest not registered: {manifest_id}"
                ) from exc

    def case(self, case_ref: str) -> TraceCaseIndex:
        with self._lock:
            try:
                return self._case_indexes[case_ref]
            except KeyError as exc:
                raise TraceStoreLookupError(
                    f"trace case not registered: {case_ref}"
                ) from exc

    def read_events(self, trace_ref_id: str, start: int, count: int) -> ReadResult:
        """Read an exact in-range normalized event window by occurrence ref."""

        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("trace read start must be a non-negative integer")
        _positive_int(count, "trace read count", maximum=100_000)
        artifact = self.artifact(trace_ref_id)
        trace = self.get(artifact.trace_ref_id)
        total = len(trace.events)
        end = start + count
        if start >= total or end > total:
            raise ValueError(
                "trace read range is outside the normalized event sequence"
            )
        content = canonical_json(trace.events[start:end])
        return ReadResult(
            ref=trace_ref_id,
            ref_kind="case",
            start=start,
            end=end,
            total=total,
            content=content,
        )

    def read_case_events(self, case_ref: str, start: int, count: int) -> ReadResult:
        """Read events using the proposer-facing opaque case ref."""

        case = self.case(case_ref)
        if case.trace_ref_id is None:
            raise TraceStoreLookupError(
                f"trace case has no readable artifact: {case_ref}"
            )
        result = self.read_events(case.trace_ref_id, start, count)
        return replace(result, ref=case_ref)


class VisibleATraceFileSink:
    """Run-bound adapter seam that publishes only normalized Visible-A traces."""

    def __init__(self, store: TraceStore, capability: AdapterTraceCapability) -> None:
        if not isinstance(store, TraceStore):
            raise TypeError("store must be TraceStore")
        if not isinstance(capability, AdapterTraceCapability):
            raise TypeError("capability must be AdapterTraceCapability")
        self.store = store
        self.capability = capability

    def capture(
        self,
        coordinates: TraceCaptureCoordinates,
        trace: NormalizedTrace,
        *,
        partial_reason: str | None = None,
    ) -> TraceCaptureResult:
        if not isinstance(coordinates, TraceCaptureCoordinates):
            raise TypeError("coordinates must be TraceCaptureCoordinates")
        if not isinstance(trace, NormalizedTrace):
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="normalized_trace_required",
            )
        if trace.trace_contract != self.capability.trace_contract:
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="trace_contract_mismatch",
            )
        if trace.partial and not self.capability.supports_timeout_partial:
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="partial_trace_not_supported",
            )
        if not trace.partial and not self.capability.supports_complete:
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="complete_trace_not_supported",
            )
        if trace.partial and not partial_reason:
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="partial_trace_reason_missing",
            )
        if not trace.partial and partial_reason is not None:
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="complete_trace_has_partial_reason",
            )
        try:
            payload = trace.canonical_bytes()
            artifact = TraceArtifactRef.create(
                coordinates=coordinates,
                format=self.capability.format,
                byte_count=len(payload),
                event_count=len(trace.events),
                trace_contract=trace.trace_contract,
            )
            self.store.put_bytes(artifact.trace_ref_id, payload)
            self.store.register_artifact(artifact)
        except (TraceStoreError, OSError, ValueError):
            return self.unavailable(
                coordinates,
                status=TraceCaptureStatus.INVALID,
                reason="trace_store_write_failed",
            )
        status = (
            TraceCaptureStatus.PARTIAL if trace.partial else TraceCaptureStatus.COMPLETE
        )
        tool_call_count, repeated_actions = summarize_trace_actions(trace)
        record = TraceCaptureRecord(
            adapter_id=self.capability.adapter_id,
            program_id=coordinates.program_id,
            cell_id=coordinates.cell_id,
            status=status,
            trace_ref_id=artifact.trace_ref_id,
            reason=partial_reason,
            tool_call_count=tool_call_count,
            repeated_action_signatures=repeated_actions,
        )
        return TraceCaptureResult(record=record, artifact_ref=artifact)

    def unavailable(
        self,
        coordinates: TraceCaptureCoordinates,
        *,
        status: TraceCaptureStatus,
        reason: str,
    ) -> TraceCaptureResult:
        if not isinstance(coordinates, TraceCaptureCoordinates):
            raise TypeError("coordinates must be TraceCaptureCoordinates")
        if status not in {TraceCaptureStatus.MISSING, TraceCaptureStatus.INVALID}:
            raise ValueError("unavailable capture status must be missing or invalid")
        record = TraceCaptureRecord(
            adapter_id=self.capability.adapter_id,
            program_id=coordinates.program_id,
            cell_id=coordinates.cell_id,
            status=status,
            trace_ref_id=None,
            reason=reason,
        )
        return TraceCaptureResult(record=record, artifact_ref=None)


__all__ = [
    "DEFAULT_MAX_TRACE_BYTES",
    "VisibleATraceFileSink",
    "TraceStore",
    "TraceStoreError",
    "TraceStoreIntegrityError",
    "TraceStoreLookupError",
    "TraceStoreSecurityError",
]
