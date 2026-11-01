"""
Utilities for exporting evolution traces to various formats
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def export_traces_jsonl(
    traces: List[Any], output_path: Union[str, Path], compress: bool = False
) -> None:
    """
    Export traces to JSONL format (one JSON object per line)

    Args:
        traces: List of trace objects with to_dict() method
        output_path: Path to save the JSONL file
        compress: Whether to compress the output with gzip
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if compress:
        import gzip

        if not output_path.suffix == ".gz":
            output_path = output_path.with_suffix(output_path.suffix + ".gz")
        open_func = gzip.open
        mode = "wt"
    else:
        open_func = open
        mode = "w"

    with open_func(output_path, mode) as f:
        for trace in traces:
            trace_dict = trace.to_dict() if hasattr(trace, "to_dict") else trace
            json.dump(trace_dict, f)
            f.write("\n")

    logger.info(f"Exported {len(traces)} traces to {output_path}")


def export_traces_json(
    traces: List[Any], output_path: Union[str, Path], metadata: Optional[Dict[str, Any]] = None
) -> None:
    """
    Export traces to JSON format with metadata

    Args:
        traces: List of trace objects with to_dict() method
        output_path: Path to save the JSON file
        metadata: Optional metadata to include in the output
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert traces to dictionaries
    trace_dicts = []
    for trace in traces:
        if hasattr(trace, "to_dict"):
            trace_dicts.append(trace.to_dict())
        else:
            trace_dicts.append(trace)

    # Build output structure
    output_data = {"metadata": metadata or {}, "traces": trace_dicts}

    # Add default metadata
    output_data["metadata"].setdefault("total_traces", len(trace_dicts))
    output_data["metadata"].setdefault("exported_at", time.time())

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    logger.info(f"Exported {len(traces)} traces to {output_path}")


def export_traces_hdf5(
    traces: List[Any], output_path: Union[str, Path], metadata: Optional[Dict[str, Any]] = None
) -> None:
    """
    Export traces to HDF5 format

    Args:
        traces: List of trace objects with to_dict() method
        output_path: Path to save the HDF5 file
        metadata: Optional metadata to include in the output
    """
    try:
        import h5py
        import numpy as np
    except ImportError:
        logger.error("h5py is required for HDF5 export. Install with: pip install h5py")
        raise ImportError("h5py not installed")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with h5py.File(output_path, "w") as f:
        # Create groups
        traces_group = f.create_group("traces")
        meta_group = f.create_group("metadata")

        # Add metadata
        if metadata:
            for key, value in metadata.items():
                if isinstance(value, (str, int, float, bool)):
                    meta_group.attrs[key] = value
                else:
                    meta_group.attrs[key] = json.dumps(value)

        meta_group.attrs["total_traces"] = len(traces)
        meta_group.attrs["exported_at"] = time.time()

        # Add traces
        for i, trace in enumerate(traces):
            trace_dict = trace.to_dict() if hasattr(trace, "to_dict") else trace
            trace_group = traces_group.create_group(f"trace_{i:06d}")

            for key, value in trace_dict.items():
                if value is None:
                    continue

                if isinstance(value, dict):
                    # Store dictionaries as JSON strings in attributes
                    trace_group.attrs[key] = json.dumps(value)
                elif isinstance(value, list):
                    # Convert lists to numpy arrays if possible
                    try:
                        arr = np.array(value)
                        trace_group.create_dataset(key, data=arr)
                    except (ValueError, TypeError):
                        # Fall back to JSON for complex lists
                        trace_group.attrs[key] = json.dumps(value)
                elif isinstance(value, str):
                    # Store strings as attributes
                    trace_group.attrs[key] = value
                elif isinstance(value, (int, float, bool)):
                    # Store scalars as attributes
                    trace_group.attrs[key] = value
                else:
                    # Store other types as JSON
                    trace_group.attrs[key] = json.dumps(value)

    logger.info(f"Exported {len(traces)} traces to {output_path}")


def append_trace_jsonl(trace: Any, output_path: Union[str, Path], compress: bool = False) -> None:
    """
    Append a single trace to a JSONL file

    Args:
        trace: Trace object with to_dict() method
        output_path: Path to the JSONL file
        compress: Whether the file is compressed with gzip
    """
    output_path = Path(output_path)

    if compress:
        import gzip

        if not output_path.suffix == ".gz":
            output_path = output_path.with_suffix(output_path.suffix + ".gz")
        open_func = gzip.open
        mode = "at"
    else:
        open_func = open
        mode = "a"

    trace_dict = trace.to_dict() if hasattr(trace, "to_dict") else trace

    with open_func(output_path, mode) as f:
        json.dump(trace_dict, f)
        f.write("\n")


def load_traces_jsonl(input_path: Union[str, Path], compress: bool = False) -> List[Dict[str, Any]]:
    """
    Load traces from a JSONL file

    Args:
        input_path: Path to the JSONL file
        compress: Whether the file is compressed with gzip

    Returns:
        List of trace dictionaries
    """
    input_path = Path(input_path)

    if compress or input_path.suffix == ".gz":
        import gzip

        open_func = gzip.open
        mode = "rt"
    else:
        open_func = open
        mode = "r"

    traces = []
    with open_func(input_path, mode) as f:
        for line in f:
            if line.strip():
                traces.append(json.loads(line))

    return traces


def load_traces_json(input_path: Union[str, Path]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Load traces from a JSON file

    Args:
        input_path: Path to the JSON file

    Returns:
        Tuple of (traces list, metadata dict)
    """
    with open(input_path, "r") as f:
        data = json.load(f)

    traces = data.get("traces", [])
    metadata = data.get("metadata", {})

    return traces, metadata


def load_traces_hdf5(input_path: Union[str, Path]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Load traces from an HDF5 file

    Args:
        input_path: Path to the HDF5 file

    Returns:
        Tuple of (traces list, metadata dict)
    """
    try:
        import h5py
    except ImportError:
        logger.error("h5py is required for HDF5 loading. Install with: pip install h5py")
        raise ImportError("h5py not installed")

    traces = []
    metadata = {}

    with h5py.File(input_path, "r") as f:
        # Load metadata
        if "metadata" in f:
            meta_group = f["metadata"]
            for key in meta_group.attrs:
                value = meta_group.attrs[key]
                # Try to parse JSON strings
                if isinstance(value, str) and value.startswith("{"):
                    try:
                        metadata[key] = json.loads(value)
                    except json.JSONDecodeError:
                        metadata[key] = value
                else:
                    metadata[key] = value

        # Load traces
        if "traces" in f:
            traces_group = f["traces"]
            for trace_name in sorted(traces_group.keys()):
                trace_group = traces_group[trace_name]
                trace_dict = {}

                # Load attributes
                for key in trace_group.attrs:
                    value = trace_group.attrs[key]
                    # Try to parse JSON strings
                    if isinstance(value, str) and (value.startswith("{") or value.startswith("[")):
                        try:
                            trace_dict[key] = json.loads(value)
                        except json.JSONDecodeError:
                            trace_dict[key] = value
                    else:
                        trace_dict[key] = value

                # Load datasets
                for key in trace_group.keys():
                    dataset = trace_group[key]
                    trace_dict[key] = dataset[...].tolist()

                traces.append(trace_dict)

    return traces, metadata


def export_traces(
    traces: List[Any],
    output_path: Union[str, Path],
    format: str = "jsonl",
    compress: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Export traces to specified format

    Args:
        traces: List of trace objects
        output_path: Path to save the file
        format: Output format ('jsonl', 'json', 'hdf5')
        compress: Whether to compress output (jsonl only)
        metadata: Optional metadata (json and hdf5 only)
    """
    format = format.lower()

    if format == "jsonl":
        export_traces_jsonl(traces, output_path, compress=compress)
    elif format == "json":
        export_traces_json(traces, output_path, metadata=metadata)
    elif format == "hdf5":
        export_traces_hdf5(traces, output_path, metadata=metadata)
    else:
        raise ValueError(f"Unsupported format: {format}. Use 'jsonl', 'json', or 'hdf5'")


def load_traces(
    input_path: Union[str, Path], format: Optional[str] = None
) -> Union[List[Dict[str, Any]], tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """
    Load traces from file, auto-detecting format if not specified

    Args:
        input_path: Path to the file
        format: Optional format ('jsonl', 'json', 'hdf5'). Auto-detected if None.

    Returns:
        For JSONL: List of trace dictionaries
        For JSON/HDF5: Tuple of (traces list, metadata dict)
    """
    input_path = Path(input_path)

    # Auto-detect format from extension
    if format is None:
        if input_path.suffix in [".jsonl", ".gz"]:
            format = "jsonl"
        elif input_path.suffix == ".json":
            format = "json"
        elif input_path.suffix in [".h5", ".hdf5"]:
            format = "hdf5"
        else:
            # Try to detect from content
            with open(input_path, "rb") as f:
                first_bytes = f.read(10)
                if first_bytes.startswith(b"\x89HDF"):
                    format = "hdf5"
                elif first_bytes.startswith(b"{"):
                    # Could be JSON or JSONL, check for newlines
                    f.seek(0)
                    content = f.read(1000)
                    if b"\n{" in content or b"\n[" in content:
                        format = "jsonl"
                    else:
                        format = "json"
                else:
                    format = "jsonl"  # Default assumption

    format = format.lower()

    if format == "jsonl":
        return load_traces_jsonl(input_path, compress=input_path.suffix == ".gz")
    elif format == "json":
        return load_traces_json(input_path)
    elif format == "hdf5":
        return load_traces_hdf5(input_path)
    else:
        raise ValueError(f"Unsupported format: {format}")
